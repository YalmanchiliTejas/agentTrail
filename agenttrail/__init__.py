"""
agenttrail/__init__.py

Public surface of the mini runtime. Keeping this tiny on purpose:
- Consumers import AgentTrail (the runtime),
- @step decorator to mark step functions,
- @compensate decorator to mark compensators for saga unwinds.
"""
from .runtime import AgentTrail, step, compensate
