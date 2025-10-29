"""
agenttrail/runtime.py

Core runtime for "Temporal for AI Agents" (demo scale). Responsibilities:
- Run lifecycle (start_run/end_run) with an append-only event log
- @step decorator to mark functions as steps; wrap() executes steps with:
  * idempotency (dedupe repeated calls),
  * OpenTelemetry spans (optional),
  * event logging and step state transitions.
- Saga compensation:
  * @compensate(for_=...) marks a compensator for a given step.
  * compensate() walks completed steps in reverse order and calls compensators.
- Deterministic replay (stub):
  * If replay=True and a prior step exists, return a replay marker rather than invoking real I/O.
  * Extend by persisting step results and tool I/O in storage.tool_calls, then reading them back.

DX upgrades in this version:
- Global auto-registration of compensators via the @compensate decorator (no manual register_compensator calls).
- AgentTrail.run(...) convenience method (handles start/end/compensate automatically).
- autowrap_namespace(...) to wrap all @step-decorated functions in a module/namespace so call sites don't change.

This file aims to be easy to read and extend. The demo keeps result persistence minimal;
a production system would store step outputs and tool I/O for true deterministic replay.
"""

import json
import uuid
from datetime import datetime
from functools import wraps
from typing import Callable, Dict, Optional, Any, Tuple

from .storage import DB
from .idempotency import idem_key, canonical_json
from . import otel

# ----------------------------
# Global registry for auto-compensators (DX addition)
# ----------------------------
# Stores pairs (target_step_function, compensator_function) recorded by @compensate.
_GLOBAL_COMP_REGISTRY = []  # list[tuple[Callable, Callable]]

# ----------------------------
# Public decorators
# ----------------------------
def step(name: Optional[str] = None):
    """
    Decorator to tag a function as a step.
    We store the step name on the function object; AgentTrail.wrap() reads it.
    """
    def decorator(fn: Callable):
        fn.__agenttrail_step_name__ = name or fn.__name__
        return fn
    return decorator

def compensate(for_: Callable):
    """
    Decorator to tag a function as a compensator for a given step function.
    Also queues it for auto-registration so callers don't need to call
    AgentTrail.register_compensator(...) manually.

    NOTE: Auto-registration binds by *step name* at runtime (honors @step("...") overrides).
    """
    def wrapper(fn: Callable):
        fn.__agenttrail_compensates__ = for_.__name__
        # DX addition: remember the pairing for auto-registration once the trail starts.
        _GLOBAL_COMP_REGISTRY.append((for_, fn))
        return fn
    return wrapper

# ----------------------------
# AgentTrail runtime
# ----------------------------
class AgentTrail:
    def __init__(self, db_path: str = ":memory:", workflow: str = "default",
                 enable_tracing: bool = True, replay: bool = False):
        """
        Args:
          db_path        : SQLite path (":memory:" for ephemeral). Swap to Postgres in prod.
          workflow       : logical workflow name; used in idempotency namespace and tracing.
          enable_tracing : optional OpenTelemetry.
          replay         : if True, prefer reading prior results over executing real logic.
        """
        self.db = DB(db_path)
        self.workflow = workflow
        self.replay = replay
        self.run_id: Optional[str] = None
        self._comp: Dict[str, Callable] = {}     # target-step-name -> compensator fn
        self.tracer = otel.init_tracing(f"agenttrail-{workflow}") if enable_tracing else None

    # ---- DX helper: resolve step name honoring @step override ----
    def _resolve_step_name(self, fn: Callable) -> str:
        return getattr(fn, "__agenttrail_step_name__", fn.__name__)

    # ---- DX helper: auto-register global compensators recorded by @compensate ----
    def auto_register_global_compensators(self):
        """
        Look at all (target_fn, comp_fn) recorded by the decorator and
        register them using the *step names* (including @step overrides).
        Safe to call multiple times.
        """
        for target_fn, comp_fn in _GLOBAL_COMP_REGISTRY:
            target_name = self._resolve_step_name(target_fn)
            self.register_compensator(target_name, comp_fn)

    # ---- Run lifecycle ----
    def start_run(self) -> str:
        """
        Create a new run row and emit RUN_STARTED. Returns the run_id for correlation.
        Also auto-registers any compensators recorded via the decorator.
        """
        self.run_id = str(uuid.uuid4())

        # DX addition: pull in all globally decorated compensators.
        self.auto_register_global_compensators()

        self.db.insert_run(
            self.run_id,
            self.workflow,
            "running",
            datetime.utcnow().isoformat(),
            determinism_hash=None,
            root_trace_id=None
        )
        self.db.log_event(self.run_id, "RUN_STARTED", {"workflow": self.workflow})
        return self.run_id

    def end_run(self, status: str = "completed"):
        """
        Mark the run as completed/failed/etc. Emits RUN_ENDED.
        """
        assert self.run_id, "No run started"
        self.db.update_run_status(self.run_id, status, datetime.utcnow().isoformat())
        self.db.log_event(self.run_id, "RUN_ENDED", {"status": status})

    # ---- DX convenience: one-liner runner handling lifecycle + compensation ----
    def run(self, workflow_fn: Callable, *args, status_on_error: str = "failed", **kwargs):
        """
        Convenience runner: start_run -> call workflow_fn -> end_run.
        On exception: compensate() then end_run(status_on_error) and re-raise.
        """
        self.start_run()
        try:
            result = workflow_fn(*args, **kwargs)
            self.end_run("completed")
            return result
        except Exception:
            try:
                self.compensate()
            finally:
                self.end_run(status_on_error)
            raise

    def register_compensator(self, target_name: str, fn: Callable):
        """
        Register a compensator function for a step (usually called at the top of the workflow).
        Example:
            @compensate(for_=reserve_funds)
            def refund_funds(...): ...
            trail.register_compensator("reserve_funds", refund_funds)

        NOTE: With the DX auto-registration, this is optional. You can still call it explicitly.
        """
        self._comp[target_name] = fn

    # ---- Step execution ----
    def _execute_step(self, fn: Callable, step_name: str, args: Tuple, kwargs: Dict):
        """
        Execute a single step with idempotency, tracing, and event emission.
        - Computes an idempotency key from (workflow, step_name, args, kwargs).
        - If a completed step exists and we're not in replay mode, return a cache marker.
        - Otherwise: create step row (status=started) -> run function -> mark completed or failed.
        """
        assert self.run_id, "Call start_run() first"
        idem = idem_key(self.workflow, step_name, args, kwargs)
        prior = self.db.get_step_by_idem(idem)

        if prior and not self.replay:
            # Idempotency hit: return a marker. In a full impl we'd also fetch result payload.
            self.db.log_event(self.run_id, "STEP_CACHE_HIT", {"step": step_name, "idem_key": idem})
            return {"cached": True, "step_id": prior["step_id"]}

        # Create a new step attempt. If prior exists, this is an explicit retry/replay.
        step_id = str(uuid.uuid4())
        attempt = 1 if prior is None else prior["attempt"] + 1
        self.db.insert_step(step_id, self.run_id, step_name, attempt, "started",
                            datetime.utcnow().isoformat(), idem)
        self.db.log_event(self.run_id, "STEP_STARTED",
                          {"step": step_name, "attempt": attempt, "idem_key": idem},
                          step_id=step_id)

        # Tracing span around the user function body for visibility.
        with otel.start_span(self.tracer, f"{self.workflow}.{step_name}"):
            try:
                if self.replay and prior:
                    # Deterministic replay stub: return a replay marker instead of calling the function.
                    # To make this "real", you'd store the step output in storage and load it here.
                    result = {"replayed": True, "step_id": prior["step_id"]}
                else:
                    result = fn(*args, **kwargs)

                self.db.update_step(step_id, "completed", datetime.utcnow().isoformat())
                self.db.log_event(self.run_id, "STEP_COMPLETED",
                                  {"step": step_name, "result": result}, step_id=step_id)
                return result
            except Exception as e:
                # Mark as failed and re-raise so the caller (workflow) can trigger compensation.
                self.db.update_step(step_id, "failed", datetime.utcnow().isoformat())
                self.db.log_event(self.run_id, "STEP_FAILED",
                                  {"step": step_name, "error": str(e)}, step_id=step_id)
                raise

    def wrap(self, fn: Callable) -> Callable:
        """
        Wrap a function marked with @step so that calling it executes through _execute_step().
        This allows you to keep business logic independent and still get runtime semantics.
        """
        step_name = getattr(fn, "__agenttrail_step_name__", fn.__name__)
        @wraps(fn)
        def inner(*args, **kwargs):
            return self._execute_step(fn, step_name, args, kwargs)
        return inner

    # ---- DX convenience: autowrap all @step-decorated functions in a namespace ----
    def autowrap_namespace(self, namespace: Dict[str, Any]):
        """
        Find functions decorated with @step in the given namespace (e.g., a module's globals())
        and replace them in place with wrapped versions so call sites remain unchanged.

        Example:
            import my_steps
            trail.autowrap_namespace(my_steps.__dict__)
        """
        for name, obj in list(namespace.items()):
            if callable(obj) and hasattr(obj, "__agenttrail_step_name__"):
                namespace[name] = self.wrap(obj)

    # ---- Compensation ----
    def compensate(self):
        """
        Walk completed steps in reverse completion order and invoke compensators for any
        steps that registered one. Each compensator itself runs as a step (prefixed with
        "compensate_") so attempts/events are tracked and idempotent.
        """
        assert self.run_id, "No run started"
        steps = self.db.pending_steps(self.run_id)
        for s in steps:
            name = s["name"]
            if name in self._comp and s["status"] == "completed":
                comp_fn = self._comp[name]
                comp_step_name = f"compensate_{name}"
                try:
                    # We pass original_step_id; in a full impl, you'd also pass the effect payload (e.g., hold_id).
                    self._execute_step(comp_fn, comp_step_name, args=(), kwargs={"original_step_id": s["step_id"]})
                    self.db.log_event(self.run_id, "STEP_COMPENSATED",
                                      {"step": name, "compensator": comp_step_name})
                except Exception as e:
                    # If compensation fails, surface it early. You may choose to continue best-effort instead.
                    self.db.log_event(self.run_id, "STEP_COMPENSATION_FAILED",
                                      {"step": name, "error": str(e)})
                    raise

    # ---- Utilities ----
    def get_run_summary(self, run_id: Optional[str] = None):
        """
        Aggregate run/steps/events for dashboards and debugging.
        """
        rid = run_id or self.run_id
        run, steps, events = self.db.run_summary(rid)
        return {
            "run": dict(run) if run else None,
            "steps": [dict(s) for s in steps],
            "events": [dict(e) for e in events],
        }
