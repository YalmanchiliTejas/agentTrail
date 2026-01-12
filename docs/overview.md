# Overview

AgentRelay provides a transactional runtime for agent workflows. It focuses on recording tool calls in a database, enforcing idempotency, and enabling deterministic replay. You bring your agent logic and LLM client; AgentRelay wraps the tools.

## Core concepts

- **AgentRuntime**: Owns the database connection and registry of tools.
- **AgentSession**: Context manager that records a single run and its tool calls.
- **Tools**: Your side-effecting functions, wrapped with the `tool` decorator.
- **Compensations**: Optional rollback tools that run if a session errors.
- **Replay**: A mode that returns stored tool outputs instead of re-running calls.

## Basic workflow

```python
from agent_relay.runtime import AgentRuntime
from agent_relay.tooling import tool

runtime = AgentRuntime.from_env()

@tool(runtime, name="send_email", compensation="cancel_email")
def send_email(to: str, subject: str, body: str) -> str:
    return "msg-123"

@tool(runtime, name="cancel_email")
def cancel_email(to: str, subject: str, body: str) -> None:
    return None


def agent_flow(event: dict) -> dict:
    msg_id = send_email(to=event["to"], subject=event["subject"], body=event["body"])
    return {"message_id": msg_id}


with runtime.agent_session(name="email_flow", input_payload={"to": "user@example.com"}) as session:
    result = agent_flow({"to": "user@example.com", "subject": "Hello", "body": "Hi"})
    session.set_output(result)
```

## Idempotency

AgentRelay computes a deterministic idempotency key for each tool call using the tool name, phase, and arguments. If the same tool call is executed again within the same run, AgentRelay returns the stored output instead of re-running the tool.

## Compensation flow

If an exception is raised inside a session, AgentRelay executes compensating tools (if registered) in reverse order. Compensation failures are best-effort and do not mask the original error.

## Replay

Replay mode allows you to run an agent without re-invoking tools. Forward tool calls are served from the `tool_calls` table instead.

```python
with runtime.agent_session(name="replay", replay=True, replay_run_id=run_id) as session:
    result = agent_flow({"to": "user@example.com", "subject": "Hello", "body": "Hi"})
    session.set_output(result)
```

## Budget enforcement

Sessions can enforce a max cost using `budget_limit`. If token usage pushes the total cost above the limit, the session raises `BudgetExceededError`.

```python
with runtime.agent_session(name="support", budget_limit=3.00) as session:
    ...
```

## Demo

See `demo/agent.py` for an end-to-end example of saga compensations, idempotent calls, and replay.
