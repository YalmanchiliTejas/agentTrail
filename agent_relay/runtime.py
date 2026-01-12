from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.exc import IntegrityError

from .context import (
    get_current_tool_call_id,
    reset_current_tool_call_id,
    set_current_session,
    set_current_tool_call_id,
)
from .db import Database
from .llm import LLMUsage

ToolCall = Callable[..., Any]


class BudgetExceededError(RuntimeError):
    pass


def _serialize_json(data: Any) -> Any:
    """Best-effort JSON serialization for logging inputs/outputs."""
    if data is None or isinstance(data, (str, int, float, bool)):
        return data
    if isinstance(data, dict):
        return {str(k): _serialize_json(v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [_serialize_json(v) for v in data]
    # Fall back to a repr so we never crash logging.
    return repr(data)


def _deserialize_json(output: Any) -> Any:
    return output


@dataclass
class ExecutedStep:
    tool_name: str
    compensation_tool_name: Optional[str]
    args: tuple
    kwargs: dict


@dataclass
class AgentRuntime:
    db: Database
    tools: Dict[str, ToolCall] = field(default_factory=dict)
    compensations: Dict[str, str] = field(default_factory=dict)

    # How long to wait for a "pending" idempotent tool call claimed by another worker/thread.
    pending_timeout_s: float = 60.0
    pending_poll_interval_s: float = 0.25

    @classmethod
    def from_connection_string(cls, conn_str: str) -> "AgentRuntime":
        db = Database.from_connection_string(conn_str)
        return cls(db=db)

    @classmethod
    def from_env(cls) -> "AgentRuntime":
        db = Database.from_env()
        return cls(db=db)

    def register_tool(self, name: str, func: ToolCall) -> None:
        self.tools[name] = func

    def register_compensation(self, tool_name: str, compensation_tool_name: str) -> None:
        self.compensations[tool_name] = compensation_tool_name

    def get_tool(self, name: str) -> ToolCall:
        return self.tools[name]

    def agent_session(
        self,
        *,
        name: str,
        input_payload: Any | None = None,
        tags: Optional[List[str]] = None,
        budget_limit: Optional[float] = None,
        compensate_on_budget_exceeded: bool = True,
        replay: bool = False,
        replay_run_id: Optional[str] = None,
        replay_calls: Optional[List[dict]] = None,
    ) -> "AgentSession":
        return AgentSession(
            runtime=self,
            name=name,
            input_payload=input_payload,
            tags=tags or [],
            budget_limit=budget_limit,
            compensate_on_budget_exceeded=compensate_on_budget_exceeded,
            replay=replay,
            replay_run_id=replay_run_id,
            replay_calls=replay_calls,
        )

    def replay_run(self, run_id: str, agent_fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        with self.agent_session(name="replay", replay=True, replay_run_id=run_id) as session:
            result = agent_fn(*args, **kwargs)
            session.set_output(result)
            return result

    def export_run(self, run_id: str) -> dict:
        run = self.db.fetchone(
            """
            SELECT id, name, status, tags, budget_limit, total_prompt_tokens, total_completion_tokens,
                   total_tokens, total_cost, input_json, output_json, error, replay_of, created_at, updated_at
            FROM agent_runs
            WHERE id = :id
            """,
            {"id": run_id},
        )
        if not run:
            raise ValueError(f"Run not found: {run_id}")

        calls = self.db.fetchall(
            """
            SELECT id, seq_no, tool_name, idempotency_key, phase, status, parent_tool_call_id, internal,
                   provider, model, request_fingerprint, prompt_tokens, completion_tokens, total_tokens,
                   input_cost, output_cost, total_cost, input_json, output_json, error, created_at, updated_at
            FROM tool_calls
            WHERE run_id = :run_id
            ORDER BY seq_no ASC
            """,
            {"run_id": run_id},
        )

        return {
            "run": dict(run._mapping),
            "tool_calls": [dict(c._mapping) for c in calls],
        }

    def replay_exported_json(self, exported: dict, agent_fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        replay_calls = exported.get("tool_calls") or []
        with self.agent_session(
            name="replay_export",
            replay=True,
            replay_run_id=str(exported.get("run", {}).get("id") or uuid.uuid4()),
            replay_calls=replay_calls,
        ) as session:
            result = agent_fn(*args, **kwargs)
            session.set_output(result)
            return result


@dataclass
class AgentSession:
    runtime: AgentRuntime
    name: str
    input_payload: Any | None = None
    tags: List[str] = field(default_factory=list)
    budget_limit: Optional[float] = None
    compensate_on_budget_exceeded: bool = True

    replay: bool = False
    replay_run_id: Optional[str] = None
    replay_calls: Optional[List[dict]] = None

    run_id: Optional[str] = None
    status: str = "pending"
    error: Optional[str] = None
    output_payload: Any | None = None

    seq_no: int = 0
    executed_steps: List[ExecutedStep] = field(default_factory=list)

    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0
    total_cost: float = 0.0

    _replay_calls: List[dict] = field(default_factory=list, init=False)
    _replay_index: int = field(default=0, init=False)
    _seq_lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __enter__(self) -> "AgentSession":
        if self.replay:
            if not self.replay_run_id:
                raise ValueError("replay_run_id must be provided for replay sessions")
            self.run_id = self.replay_run_id
            if self.replay_calls is not None:
                self._replay_calls = list(self.replay_calls)
            else:
                self._load_replay_calls()
            set_current_session(self)
            return self

        self.run_id = str(uuid.uuid4())
        now = datetime.utcnow()
        self.runtime.db.execute(
            """
            INSERT INTO agent_runs (
                id, name, status, tags, budget_limit,
                total_prompt_tokens, total_completion_tokens, total_tokens, total_cost,
                input_json, created_at, updated_at
            ) VALUES (
                :id, :name, :status, :tags, :budget_limit,
                :pt, :ct, :tt, :tc,
                :input_json, :created_at, :updated_at
            )
            """,
            {
                "id": self.run_id,
                "name": self.name,
                "status": self.status,
                "tags": json.dumps(self.tags) if self.tags else None,
                "budget_limit": self.budget_limit,
                "pt": int(self.total_prompt_tokens),
                "ct": int(self.total_completion_tokens),
                "tt": int(self.total_tokens),
                "tc": float(self.total_cost),
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

                should_compensate = not self.replay
                if exc_type is BudgetExceededError and not self.compensate_on_budget_exceeded:
                    should_compensate = False

                if should_compensate:
                    self._run_compensations()

            self._persist_final_status()
        finally:
            set_current_session(None)

    def set_output(self, value: Any) -> None:
        self.output_payload = value

    def _persist_final_status(self) -> None:
        if not self.run_id:
            return

        self.runtime.db.execute(
            """
            UPDATE agent_runs
            SET status = :status,
                output_json = :output_json,
                error = :error,
                total_prompt_tokens = :pt,
                total_completion_tokens = :ct,
                total_tokens = :tt,
                total_cost = :tc,
                updated_at = :updated_at
            WHERE id = :id
            """,
            {
                "id": self.run_id,
                "status": self.status,
                "output_json": json.dumps(_serialize_json(self.output_payload)),
                "error": self.error,
                "pt": int(self.total_prompt_tokens),
                "ct": int(self.total_completion_tokens),
                "tt": int(self.total_tokens),
                "tc": float(self.total_cost),
                "updated_at": datetime.utcnow(),
            },
        )

    def _load_replay_calls(self) -> None:
        if not self.run_id:
            return

        rows = self.runtime.db.fetchall(
            """
            SELECT seq_no, tool_name, phase, input_json, output_json, status, error
            FROM tool_calls
            WHERE run_id = :run_id
            ORDER BY seq_no ASC
            """,
            {"run_id": self.run_id},
        )
        self._replay_calls = [dict(r._mapping) for r in rows]

    # ---- core tool execution API used by the decorator ----

    def execute_tool_call(
        self,
        *,
        tool_name: str,
        func: ToolCall,
        args: tuple,
        kwargs: dict,
        phase: str = "forward",
        compensation_tool_name: Optional[str] = None,
        parent_tool_call_id: Optional[str] = None,
        internal: bool = False,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        request_fingerprint: Optional[str] = None,
        usage_parser: Optional[Callable[[Any], LLMUsage]] = None,
        input_kwargs: Optional[dict] = None,
    ) -> Any:
        if not self.run_id:
            raise RuntimeError("AgentSession has no run_id; did you use it as a context manager?")

        if self.replay:
            return self._replay_step(tool_name, phase)

        if phase == "forward" and self._is_budget_exceeded():
            raise BudgetExceededError(
                f"Budget cap exceeded: total_cost={self.total_cost} limit={self.budget_limit}"
            )

        logged_kwargs = input_kwargs if input_kwargs is not None else kwargs
        idem_key = self._compute_idempotency_key(tool_name, args, logged_kwargs, phase)

        with self._seq_lock:
            next_seq_no = self.seq_no + 1

        call_id = str(uuid.uuid4())
        now = datetime.utcnow()
        parent_id = parent_tool_call_id or get_current_tool_call_id()

        # Try to claim the idempotent call by inserting a "pending" row.
        try:
            self.runtime.db.execute(
                """
                INSERT INTO tool_calls (
                    id, run_id, seq_no, tool_name, idempotency_key,
                    phase, status,
                    parent_tool_call_id, internal,
                    provider, model, request_fingerprint,
                    input_json, created_at, updated_at
                ) VALUES (
                    :id, :run_id, :seq_no, :tool_name, :idem,
                    :phase, :status,
                    :parent_tool_call_id, :internal,
                    :provider, :model, :request_fingerprint,
                    :input_json, :created_at, :updated_at
                )
                """,
                {
                    "id": call_id,
                    "run_id": self.run_id,
                    "seq_no": next_seq_no,
                    "tool_name": tool_name,
                    "idem": idem_key,
                    "phase": phase,
                    "status": "pending",
                    "parent_tool_call_id": parent_id,
                    "internal": 1 if internal else 0,
                    "provider": provider,
                    "model": model,
                    "request_fingerprint": request_fingerprint,
                    "input_json": json.dumps(_serialize_json({"args": args, "kwargs": logged_kwargs})),
                    "created_at": now,
                    "updated_at": now,
                },
            )
            # Only advance sequence if we actually claimed.
            with self._seq_lock:
                self.seq_no = next_seq_no
        except IntegrityError:
            # Another worker/thread already claimed this same idempotent call.
            return self._wait_for_existing_call(tool_name, idem_key, phase)

        if phase == "forward":
            self.executed_steps.append(
                ExecutedStep(
                    tool_name=tool_name,
                    compensation_tool_name=compensation_tool_name,
                    args=args,
                    kwargs=kwargs,
                )
            )

        token = set_current_tool_call_id(call_id)
        try:
            output = func(*args, **kwargs)
            usage: Optional[LLMUsage] = usage_parser(output) if usage_parser else None

            self.runtime.db.execute(
                """
                UPDATE tool_calls
                SET status = 'success',
                    output_json = :output_json,
                    prompt_tokens = :prompt_tokens,
                    completion_tokens = :completion_tokens,
                    total_tokens = :total_tokens,
                    input_cost = :input_cost,
                    output_cost = :output_cost,
                    total_cost = :total_cost,
                    updated_at = :updated_at
                WHERE id = :id
                """,
                {
                    "id": call_id,
                    "output_json": json.dumps(_serialize_json(output)),
                    "prompt_tokens": getattr(usage, "prompt_tokens", None),
                    "completion_tokens": getattr(usage, "completion_tokens", None),
                    "total_tokens": getattr(usage, "total_tokens", None),
                    "input_cost": getattr(usage, "input_cost", None),
                    "output_cost": getattr(usage, "output_cost", None),
                    "total_cost": getattr(usage, "total_cost", None),
                    "updated_at": datetime.utcnow(),
                },
            )

            if usage is not None:
                self._record_usage_totals(usage)

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
        finally:
            reset_current_tool_call_id(token)

    def execute_llm_call(
        self,
        *,
        provider: str,
        model: str,
        tool_name: str,
        call: Callable[[], Any],
        usage_parser: Callable[[Any], LLMUsage],
        request_fingerprint: Optional[str] = None,
    ) -> Any:
        logged_kwargs = {"request_fingerprint": request_fingerprint}
        return self.execute_tool_call(
            tool_name=tool_name,
            func=lambda: call(),
            args=(),
            kwargs={},
            phase="forward",
            compensation_tool_name=None,
            parent_tool_call_id=get_current_tool_call_id(),
            internal=True,
            provider=provider,
            model=model,
            request_fingerprint=request_fingerprint,
            usage_parser=usage_parser,
            input_kwargs=logged_kwargs,
        )

    def _wait_for_existing_call(self, tool_name: str, idem_key: str, phase: str) -> Any:
        deadline = time.time() + float(self.runtime.pending_timeout_s)
        while True:
            row = self.runtime.db.fetchone(
                """
                SELECT status, output_json, error
                FROM tool_calls
                WHERE run_id = :run_id
                  AND tool_name = :tool_name
                  AND idempotency_key = :idem
                  AND phase = :phase
                """,
                {
                    "run_id": self.run_id,
                    "tool_name": tool_name,
                    "idem": idem_key,
                    "phase": phase,
                },
            )

            if not row:
                raise RuntimeError("Idempotent call exists but row could not be loaded")

            if row.status == "success":
                if row.output_json is None:
                    return None
                return _deserialize_json(json.loads(row.output_json))

            if row.status == "error":
                raise RuntimeError(f"Prior attempt failed: {row.error}")

            # pending
            if time.time() > deadline:
                raise TimeoutError(
                    f"Timed out waiting for pending tool call: {tool_name}/{phase}"
                )

            time.sleep(float(self.runtime.pending_poll_interval_s))

    def _compute_idempotency_key(self, tool_name: str, args: tuple, kwargs: dict, phase: str) -> str:
        payload = {"tool": tool_name, "phase": phase, "args": args, "kwargs": kwargs}
        json_str = json.dumps(payload, sort_keys=True, default=repr)
        return hashlib.sha256(json_str.encode("utf-8")).hexdigest()

    def _record_usage_totals(self, usage: LLMUsage) -> None:
        self.total_prompt_tokens += int(usage.prompt_tokens)
        self.total_completion_tokens += int(usage.completion_tokens)
        self.total_tokens += int(usage.total_tokens)
        self.total_cost = round(float(self.total_cost) + float(usage.total_cost), 6)
        if self._is_budget_exceeded():
            raise BudgetExceededError(
                f"Budget cap exceeded: total_cost={self.total_cost} limit={self.budget_limit}"
            )

    def _is_budget_exceeded(self) -> bool:
        if self.budget_limit is None:
            return False
        return float(self.total_cost) > float(self.budget_limit)

    def _replay_step(self, tool_name: str, phase: str) -> Any:
        if self._replay_index >= len(self._replay_calls):
            raise RuntimeError("Replay exceeded recorded tool calls")

        record = self._replay_calls[self._replay_index]
        self._replay_index += 1

        if record.get("tool_name") != tool_name or record.get("phase") != phase:
            raise RuntimeError(
                f"Replay mismatch. Expected {tool_name}/{phase}, got {record.get('tool_name')}/{record.get('phase')}"
            )

        if record.get("status") != "success":
            raise RuntimeError(f"Replayed tool call ended in status {record.get('status')}")

        out = record.get("output_json")
        if out is None:
            return None

        # If exported JSON already contains decoded objects, accept them.
        if isinstance(out, (dict, list, str, int, float, bool)):
            return _deserialize_json(out)

        return _deserialize_json(json.loads(out))

    def _run_compensations(self) -> None:
        for step in reversed(self.executed_steps):
            if not step.compensation_tool_name:
                continue
            try:
                comp_fn = self.runtime.get_tool(step.compensation_tool_name)
                self.execute_tool_call(
                    tool_name=step.compensation_tool_name,
                    func=comp_fn,
                    args=step.args,
                    kwargs=step.kwargs,
                    phase="compensation",
                    compensation_tool_name=None,
                )
            except Exception:
                # Best-effort: compensation failures shouldn't mask the original error.
                continue
