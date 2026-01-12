# Reference

## Environment variables

AgentRelay reads the first available database URL in this order:

- `AGENTTRAIL_DB_URL`
- `AGENTTRAIL_DATABASE_URL`
- `DATABASE_URL`

If none are set, it defaults to a local SQLite DB at `./agenttrail.db`.

## Database schema highlights

The schema includes two primary tables:

- `agent_runs`: one row per session, with status, input/output JSON, tags, and total cost fields.
- `tool_calls`: one row per tool invocation, including idempotency key, phase, status, IO JSON, and LLM usage metadata.

Each tool call is uniquely indexed by `(run_id, tool_name, idempotency_key, phase)` to enforce idempotency.

## Runtime helpers

### Export a run

```python
exported = runtime.export_run(run_id)
```

This returns a dictionary with the run record and ordered tool calls. You can store this JSON to replay later.

### Replay from export

```python
result = runtime.replay_exported_json(exported, agent_fn, *args, **kwargs)
```

The exported calls are used to simulate tool outputs without invoking the original tools.

### Replay by run ID

```python
result = runtime.replay_run(run_id, agent_fn, *args, **kwargs)
```

## Errors

- `BudgetExceededError`: raised when the total cost exceeds `budget_limit` during a forward run.
