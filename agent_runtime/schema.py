SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agent_runs (
    id UUID PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    input_json JSONB,
    output_json JSONB,
    error TEXT,
    replay_of UUID NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_name
    ON agent_runs (name);

CREATE TABLE IF NOT EXISTS tool_calls (
    id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    seq_no INTEGER NOT NULL,
    tool_name TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    phase TEXT NOT NULL,        -- "forward" or "compensation"
    status TEXT NOT NULL,       -- "pending", "success", "error"
    input_json JSONB,
    output_json JSONB,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, tool_name, idempotency_key, phase)
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_run_seq
    ON tool_calls (run_id, seq_no);
"""