from __future__ import annotations

from contextvars import ContextVar
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .agentRuntime import AgentSession

_current_session: ContextVar[Optional["AgentSession"]] = ContextVar(
    "_current_agent__seesion", default=None
)

def get_current_session() -> Optional["AgentSession"]:
    return _current_session.get()
def set_current_session(session: Optional["AgentSession"]) -> None:
    _current_session.set(session)

