# Agenttrail

Agenttrail is a framework-agnostic, transactional runtime for AI agents and tool-based workflows.

It gives you:

- **Tool-call idempotency** – prevent duplicate side effects (emails, payments, DB writes) even when your code retries.
- **Saga-style compensations** – register compensating tools that run automatically on failure to roll back partial work.
- **Deterministic replay** – re-run an agent workflow using recorded tool outputs instead of calling external systems again.
- **Framework-agnostic SDK** – plug into any LLM / agent stack (OpenAI, Gemini, your own code) using a small Python SDK.
- **SQL-backed durability** – store runs and tool calls in Postgres or MySQL with a simple schema.

Agenttrail is designed for “production-style” agent workflows in domains like fintech, healthcare, and operations, where you care about not double-charging users, not sending emails twice, and being able to debug and audit what an agent actually did.

---

## Features

- **Idempotent tool calls**
  - Each tool invocation is assigned a deterministic idempotency key based on tool name, phase, and arguments.
  - A unique index at the DB layer enforces “do not run the same tool call twice for a given run”.
  - If the same call is retried, Agenttrail returns the previously persisted output instead of re-invoking the tool.

- **Saga-style compensations**
  - Tools can register a corresponding “compensation” tool.
  - On failure, Agenttrail walks executed steps in reverse order and triggers compensation calls.
  - Best-effort reversals: compensation failures are logged but do not crash the process again.

- **Deterministic replay**
  - You can replay a past run by opening a session in replay mode.
  - Forward-phase tool calls are served from the `tool_calls` table instead of calling external APIs or LLMs again.
  - This makes debugging and auditing easier and avoids re-running side effects.

- **Framework-agnostic**
  - Agenttrail does not depend on any specific LLM or agent framework.
  - You bring your own agent code and LLM client (OpenAI, Gemini, etc.).
  - Agenttrail just wraps tool calls and persists the workflow state.

---

## Installation

Once published to PyPI:

```bash
pip install agenttrail
