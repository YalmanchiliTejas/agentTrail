# AgentTrail (demo)

A minimal, framework-agnostic runtime for agent workflows with:
- Idempotent step decorator
- Saga-style compensation
- Deterministic replay stub (returns cached step where available)
- Optional OpenTelemetry tracing (no-op if not installed)
- SQLite event store with WAL

## Why these files exist

- **agenttrail/runtime.py** — Orchestrates step execution, idempotency, tracing, and compensation.
- **agenttrail/storage.py** — Encapsulates DB schema + operations. WAL makes demo concurrency workable.
- **agenttrail/idempotency.py** — Generates stable keys with type tags and canonical JSON.
- **agenttrail/otel.py** — Optional tracing that gracefully degrades if OTEL isn't present.
- **examples/payment_demo.py** — End-to-end flow that fails and compensates to showcase behavior.

## Quick start

```bash
unzip agenttrail_demo.zip
cd agenttrail_demo
python -m examples.payment_demo
```

Re-run the demo to see idempotency (cache hits). Edit `place_order` to not fail and see a successful run.

## Next steps

- Persist step results and tool I/O to implement full deterministic replay.
- Add an "outbox dispatcher" process that reads `outbox` and delivers side-effects exactly once.
- Swap SQLite for Postgres and enable row-level multitenancy (tenant_id column).
