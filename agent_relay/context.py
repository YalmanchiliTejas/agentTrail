from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .runtime import AgentSession

_current_session: ContextVar[Optional["AgentSession"]] = ContextVar(
    "_current_agent_session", default=None
)

_current_tool_call_id: ContextVar[Optional[str]] = ContextVar(
    "_current_tool_call_id", default=None
)


def get_current_session() -> Optional["AgentSession"]:
    return _current_session.get()


def set_current_session(session: Optional["AgentSession"]) -> None:
    _current_session.set(session)


def get_current_tool_call_id() -> Optional[str]:
    return _current_tool_call_id.get()


def set_current_tool_call_id(tool_call_id: str) -> Token:
    return _current_tool_call_id.set(tool_call_id)


def reset_current_tool_call_id(token: Token) -> None:
    _current_tool_call_id.reset(token)
