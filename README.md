# AgentRelay

AgentRelay is a framework-agnostic, transactional runtime for AI agents and tool-based workflows.

It gives you:

- **Tool-call idempotency** – prevent duplicate side effects (emails, payments, DB writes) even when your code retries.
- **Saga-style compensations** – register compensating tools that run automatically on failure to roll back partial work.
- **Deterministic replay** – re-run an agent workflow using recorded tool outputs instead of calling external systems again.
- **Framework-agnostic SDK** – plug into any LLM / agent stack (OpenAI, Gemini, your own code) using a small Python SDK.
- **SQL-backed durability** – store runs and tool calls in Postgres, MySQL, or SQLite with a simple schema.
- **LLM cost and token tracking** – capture prompt/completion token usage and budget caps with helper wrappers.

AgentRelay is designed for “production-style” agent workflows in domains like fintech, healthcare, and operations, where you care about not double-charging users, not sending emails twice, and being able to debug and audit what an agent actually did.

---

## Documentation

- [Overview and concepts](docs/overview.md)
- [LLM usage tracking](docs/llm-usage.md)
- [Reference and exports](docs/reference.md)

---

## Features

- **Idempotent tool calls**
  - Each tool invocation is assigned a deterministic idempotency key based on tool name, phase, and arguments.
  - A unique index at the DB layer enforces “do not run the same tool call twice for a given run”.
  - If the same call is retried, AgentRelay returns the previously persisted output instead of re-invoking the tool.

- **Saga-style compensations**
  - Tools can register a corresponding “compensation” tool.
  - On failure, AgentRelay walks executed steps in reverse order and triggers compensation calls.
  - Best-effort reversals: compensation failures are logged but do not crash the process again.

- **Deterministic replay**
  - You can replay a past run by opening a session in replay mode.
  - Forward-phase tool calls are served from the `tool_calls` table instead of calling external APIs or LLMs again.
  - This makes debugging and auditing easier and avoids re-running side effects.

- **Framework-agnostic**
  - AgentRelay does not depend on any specific LLM or agent framework.
  - You bring your own agent code and LLM client (OpenAI, Gemini, etc.).
  - AgentRelay just wraps tool calls and persists the workflow state.

- **LLM usage tracking**
  - Wrap LLM calls to store provider, model, token counts, and costs.
  - Enforce a per-run budget limit that raises a `BudgetExceededError` before spend grows unbounded.

---

## Installation

Once published to PyPI:

```bash
pip install agentrelay
```

## Quickstart

```python
from agent_relay.runtime import AgentRuntime
from agent_relay.tooling import tool

runtime = AgentRuntime.from_env()

@tool(runtime, name="charge_card", compensation="refund_card")
def charge_card(amount_cents: int, card_id: str) -> str:
    # call your payment provider
    return "payment-id-123"

@tool(runtime, name="refund_card")
def refund_card(amount_cents: int, card_id: str) -> None:
    # undo the charge
    return None


def billing_agent(event: dict) -> dict:
    payment_id = charge_card(amount_cents=event["amount_cents"], card_id=event["card_id"])
    return {"payment_id": payment_id}


with runtime.agent_session(name="billing", input_payload={"amount_cents": 2500, "card_id": "card-1"}) as session:
    result = billing_agent({"amount_cents": 2500, "card_id": "card-1"})
    session.set_output(result)
```

## Database setup

AgentRelay will connect to the database specified in `AGENTTRAIL_DB_URL`, `AGENTTRAIL_DATABASE_URL`, or `DATABASE_URL`. If none are set, it defaults to a local SQLite file (`./agenttrail.db`).

You can also build connection strings manually:

```python
from agent_relay.db import sqlite_connection_string, mysql_connection_string

sqlite_url = sqlite_connection_string("./agenttrail.db")
mysql_url = mysql_connection_string(user="agenttrail", password="agenttrail", host="localhost", database="agenttrail")
```

## Budget limits

```python
with runtime.agent_session(
    name="support_agent",
    budget_limit=2.00,
    compensate_on_budget_exceeded=False,
) as session:
    ...
```

## LLM wrappers

```python
from agent_relay.llm import wrap_openai_call

response = wrap_openai_call(
    model="gpt-4o-mini",
    input_cost_per_1k=0.15,
    output_cost_per_1k=0.60,
    request_payload={"messages": [{"role": "user", "content": "Hello"}]},
    call=lambda: client.responses.create(model="gpt-4o-mini", input="Hello"),
)
```

## Replay and exports

```python
exported = runtime.export_run(run_id)
replayed = runtime.replay_exported_json(exported, billing_agent, {"amount_cents": 2500, "card_id": "card-1"})
```

## Demos

The `demo/agent.py` script shows saga compensation, idempotency, and replay patterns.

---

## Coming soon

- **An online dashboard for effective debugging**
- **A cloud version (so no need of setup on your end, just call the library with the API and get going!)**
