from __future__ import annotations

from functools import wraps
from typing import Any, Callable, Optional
from .context import get_current_session
from .runtime import AgentRuntime

ToolCall = Callable[..., Any]
def tool(runtime: AgentRuntime, name: Optional[str]= None, compensation: Optional[str]=None)-> Callable[[ToolCall], ToolCall]:
    def decorator(func:ToolCall)-> ToolCall:
        tool_name = name or func.__name__
        runtime.register_tool(tool_name, func)
        if compensation:
            runtime.register_compensation_tool(tool_name, compensation)
        @wraps(func)
        def wrapper(*args:Any, **kwargs:Any)-> Any:
            session = get_current_session()
            if session is None:
                return func(*args, **kwargs)
            compensation_name = runtime.compensations.get(tool_name) if session.replay_mode else None
            return session.execute_tool_call(
                tool_name, func, args, kwargs, compensation_name, phase="forward")
        return wrapper
    return decorator