from __future__ import annotations
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional
import hashlib

from .db import Database
from .context import get_current_session, set_current_session

ToolCall = Callable[..., Any]

def _serialize_json(data: Any) -> str:
    return json.dumps(data)

def _deserialize_json(data: Any) -> Any:

    if isinstance(data, dict) and data.get("__non_json__") is True:
        return data.get("repr")
    return data
@dataclass
class ExecutedStep:
    tool_name: str
    compensation_tool_name: Optional[str]
    args:tuple
    kwargs:dict

@dataclass
class AgentRuntime:
    db:Database
    tools: Dict[str, ToolCall] = field(default_factory=dict)
    compensations: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_conection_string(cls, conn_str: str) -> "AgentRuntime":
        db = Database.from_connection_string(conn_str)
        return cls(db=db)
    def register_tool(self, name: str, func: ToolCall) -> None:
        self.tools[name] = func
    def register_compensation(self, tool_name: str, compensation_tool_name: str) -> None:
        self.compensations[tool_name] = compensation_tool_name
    def get_tool(self, name: str)-> ToolCall:
        if name not in self.tools:
            raise ValueError(f"Tool '{name}' is not registered.")
        return self.tools[name]
    def agent_session(self, name: str, input_payload: Any|None = None, replay:bool=False, replay_run_id: Optional[str] = None)->"AgentSession":
        return AgentSession(runtime=self, name = name, input_payload=input_payload, replay=replay, replay_run_id=replay_run_id)
    def replay_run(
        self,
        name: str,
        run_id: str,
        agent_fn: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """
        Deterministically re-run an agent function using the recorded tool_calls
        for `run_id`.

        - Opens an AgentSession in replay mode.
        - All decorated tool calls are served from the tool_calls table
          via AgentSession._replay_step instead of re-invoking the real tools.
        """
        with self.agent_session(
            name=name,
            input_payload={"replay_of": run_id},
            replay=True,
            replay_run_id=run_id,
        ) as session:
            result = agent_fn(*args, **kwargs)
            session.set_output(result)
            # We return whatever the agent function produced
            return result
@dataclass
class AgentSession:
    runtime: AgentRuntime
    name: str
    input_payload: Any | None
    replay: bool = False
    replay_run_id: Optional[str] = None

    run_id: str | None = None
    status: str = "pending"
    error: Optional[str] = None
    output_payload: Any | None = None

    seq_no: int = 0
    executed_steps: List[ExecutedStep] = field(default_factory=list)
    _replay_calls: List[dict] = field(default_factory=list)
    _replay_index: int = 0

    def __enter__(self) -> "AgentSession":
        if self.replay:
            if not self.replay_run_id:
                raise ValueError("replay_run_id must be provided for replay sessions")
            self.run_id = self.replay_run_id
            self._load_replay_calls()
            set_current_session(self)
            return self

        # create new agent_runs row
        self.run_id = str(uuid.uuid4())
        sql = """
            INSERT INTO agent_runs (id, name, status, input_json, created_at, updated_at)
            VALUES (:id, :name, :status, :input_json, :created_at, :updated_at)
        """
        now = datetime.utcnow()
        self.runtime.db.execute(
            sql,
            {
                "id": self.run_id,
                "name": self.name,
                "status": "running",
                "input_json": json.dumps(_serialize_json(self.input_payload)),
                "created_at": now,
                "updated_at": now,
            },
        )
        set_current_session(self)
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        try:
            if exc_type is None:
                self.status = "success"
            else:
                self.status = "error"
                self.error = str(exc_value) if exc_value else "unknown error"
                # run compensations if not in replay mode
                if not self.replay:
                    self._run_compensations()
            self._persist_final_status()
        finally:
            # clear contextvar even if DB write fails
            set_current_session(None)

    def set_output(self, value: Any) -> None:
        self.output_payload = value

    # ---- internal persistence helpers ----

    def _persist_final_status(self) -> None:
        if not self.run_id:
            return
        sql = """
            UPDATE agent_runs
            SET status = :status,
                output_json = :output_json,
                error = :error,
                updated_at = :updated_at
            WHERE id = :id
        """
        self.runtime.db.execute(
            sql,
            {
                "id": self.run_id,
                "status": self.status,
                "output_json": json.dumps(_serialize_json(self.output_payload)),
                "error": self.error,
                "updated_at": datetime.utcnow(),
            },
        )

    def _load_replay_calls(self) -> None:
        sql = """
            SELECT seq_no, tool_name, phase, input_json, output_json, status, error
            FROM tool_calls
            WHERE run_id = :run_id
            ORDER BY seq_no ASC
        """
        rows = self.runtime.db.fetchall(sql, {"run_id": self.run_id})
        self._replay_calls = [
            {
                "seq_no": r.seq_no,
                "tool_name": r.tool_name,
                "phase": r.phase,
                "input_json": r.input_json,
                "output_json": r.output_json,
                "status": r.status,
                "error": r.error,
            }
            for r in rows
        ]

    # ---- core tool execution API used by the decorator ----

    def execute_tool_call(
        self,
        tool_name: str,
        func: ToolCall,
        args: tuple,
        kwargs: dict,
        phase: str = "forward",
        compensation_tool_name: Optional[str] = None,
    ) -> Any:
        if not self.run_id:
            raise RuntimeError("AgentSession has no run_id; did you use it as a context manager?")

        if self.replay:
            print(f"[REPLAY MODE] serving {tool_name}/{phase} from DB")
            return self._replay_step(tool_name, phase)

        idem_key = self._compute_idempotency_key(tool_name, args, kwargs, phase)

        # check for existing successful call (idempotency)
        existing = self.runtime.db.fetchone(
            """
            SELECT output_json
            FROM tool_calls
            WHERE run_id = :run_id
              AND tool_name = :tool_name
              AND idempotency_key = :idem
              AND phase = :phase
              AND status = 'success'
            """,
            {
                "run_id": self.run_id,
                "tool_name": tool_name,
                "idem": idem_key,
                "phase": phase,
            },
        )
        if existing:
            output = json.loads(existing.output_json)
            return _deserialize_json(output)

        # new call
        self.seq_no += 1
        call_id = str(uuid.uuid4())
        now = datetime.utcnow()

        self.runtime.db.execute(
            """
            INSERT INTO tool_calls (
                id, run_id, seq_no, tool_name, idempotency_key,
                phase, status, input_json, created_at, updated_at
            )
            VALUES (
                :id, :run_id, :seq_no, :tool_name, :idem,
                :phase, :status, :input_json, :created_at, :updated_at
            )
            """,
            {
                "id": call_id,
                "run_id": self.run_id,
                "seq_no": self.seq_no,
                "tool_name": tool_name,
                "idem": idem_key,
                "phase": phase,
                "status": "pending",
                "input_json": json.dumps(_serialize_json({"args": args, "kwargs": kwargs})),
                "created_at": now,
                "updated_at": now,
            },
        )

        if phase == "forward":
            self.executed_steps.append(
                ExecutedStep(
                    tool_name=tool_name,
                    compensation_tool_name=compensation_tool_name,
                    args=args,
                    kwargs=kwargs,
                )
            )

        # run the actual tool
        try:
            output = func(*args, **kwargs)
            self.runtime.db.execute(
                """
                UPDATE tool_calls
                SET status = 'success',
                    output_json = :output_json,
                    updated_at = :updated_at
                WHERE id = :id
                """,
                {
                    "id": call_id,
                    "output_json": json.dumps(_serialize_json(output)),
                    "updated_at": datetime.utcnow(),
                },
            )
            return output
        except Exception as e:
            self.runtime.db.execute(
                """
                UPDATE tool_calls
                SET status = 'error',
                    error = :error,
                    updated_at = :updated_at
                WHERE id = :id
                """,
                {
                    "id": call_id,
                    "error": str(e),
                    "updated_at": datetime.utcnow(),
                },
            )
            raise

    def _compute_idempotency_key(
        self, tool_name: str, args: tuple, kwargs: dict, phase: str
    ) -> str:
        payload = {"tool": tool_name, "phase": phase, "args": args, "kwargs": kwargs}
        # simple deterministic JSON then hash
        json_str = json.dumps(payload, sort_keys=True, default=repr)
        # you can swap in a real hash; for MVP string is fine
        digest = hashlib.sha256(json_str.encode("utf-8")).hexdigest()
        return digest

    def _replay_step(self, tool_name: str, phase: str) -> Any:
        if self._replay_index >= len(self._replay_calls):
            raise RuntimeError("Replay exceeded recorded tool calls")

        record = self._replay_calls[self._replay_index]
        self._replay_index += 1

        if record["tool_name"] != tool_name or record["phase"] != phase:
            raise RuntimeError(
                f"Replay mismatch. Expected {tool_name}/{phase}, "
                f"got {record['tool_name']}/{record['phase']}"
            )

        if record["status"] != "success":
            raise RuntimeError(f"Replayed tool call ended in status {record['status']}")

        output = json.loads(record["output_json"])
        return _deserialize_json(output)

    def _run_compensations(self) -> None:
        # best-effort: run compensations in reverse order
        for step in reversed(self.executed_steps):
            comp_name = step.compensation_tool_name
            if not comp_name:
                continue

            comp_func = self.runtime.get_tool(comp_name)
            try:
                self.execute_tool_call(
                    tool_name=comp_name,
                    func=comp_func,
                    args=step.args,
                    kwargs=step.kwargs,
                    phase="compensation",
                    compensation_tool_name=None,
                )
            except Exception:
                # in MVP we swallow compensation failures
                continue
