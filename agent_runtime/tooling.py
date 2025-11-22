from __future__ import annotations

from functools import wraps
from typing import Any, Callable, Optional
from .context import get_current_session
from .runtime import AgentRuntime

ToolCall = Callable[..., Any]
def tool(
    runtime: AgentRuntime,
    name: Optional[str] = None,
    compensation: Optional[str] = None,
) -> Callable[[ToolCall], ToolCall]:
    def decorator(func: ToolCall) -> ToolCall:
        tool_name = name or func.__name__

        # Register the forward tool
        runtime.register_tool(tool_name, func)

        # Optionally register the compensation mapping
        if compensation:
            runtime.register_compensation(tool_name, compensation)

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            session = get_current_session()

            # If we’re not inside an AgentSession context, just call it normally
            if session is None:
                return func(*args, **kwargs)

            # Only schedule compensations for forward executions, not replay
            compensation_name = (
                runtime.compensations.get(tool_name)
                if not session.replay
                else None
            )

            return session.execute_tool_call(
                tool_name=tool_name,
                func=func,
                args=args,
                kwargs=kwargs,
                phase="forward",
                compensation_tool_name=compensation_name,
            )

        return wrapper  # type: ignore[return-value]

    return decorator