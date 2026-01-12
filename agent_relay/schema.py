from __future__ import annotations

# OSS/local runtime uses SQLite (default) or MySQL.
# The cloud backend often uses Postgres.

SCHEMA_SQL_POSTGRES = """
CREATE TABLE IF NOT EXISTS agent_runs (
    id UUID PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    tags JSONB,
    budget_limit DOUBLE PRECISION,
    total_prompt_tokens BIGINT NOT NULL DEFAULT 0,
    total_completion_tokens BIGINT NOT NULL DEFAULT 0,
    total_tokens BIGINT NOT NULL DEFAULT 0,
    total_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
    input_json JSONB,
    output_json JSONB,
    error TEXT,
    replay_of UUID NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_name ON agent_runs (name);
CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON agent_runs (status);

CREATE TABLE IF NOT EXISTS tool_calls (
    id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    seq_no INTEGER NOT NULL,
    tool_name TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    phase TEXT NOT NULL,
    status TEXT NOT NULL,
    parent_tool_call_id UUID NULL,
    internal BOOLEAN NOT NULL DEFAULT FALSE,
    provider TEXT NULL,
    model TEXT NULL,
    request_fingerprint TEXT NULL,
    prompt_tokens BIGINT NULL,
    completion_tokens BIGINT NULL,
    total_tokens BIGINT NULL,
    input_cost DOUBLE PRECISION NULL,
    output_cost DOUBLE PRECISION NULL,
    total_cost DOUBLE PRECISION NULL,
    input_json JSONB,
    output_json JSONB,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, tool_name, idempotency_key, phase)
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_run_seq ON tool_calls (run_id, seq_no);
CREATE INDEX IF NOT EXISTS idx_tool_calls_parent ON tool_calls (parent_tool_call_id);
"""

SCHEMA_SQL_SQLITE = """
CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    tags TEXT,
    budget_limit REAL,
    total_prompt_tokens INTEGER NOT NULL DEFAULT 0,
    total_completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    total_cost REAL NOT NULL DEFAULT 0,
    input_json TEXT,
    output_json TEXT,
    error TEXT,
    replay_of TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_name ON agent_runs (name);
CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON agent_runs (status);

CREATE TABLE IF NOT EXISTS tool_calls (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    seq_no INTEGER NOT NULL,
    tool_name TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    phase TEXT NOT NULL,
    status TEXT NOT NULL,
    parent_tool_call_id TEXT,
    internal INTEGER NOT NULL DEFAULT 0,
    provider TEXT,
    model TEXT,
    request_fingerprint TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    input_cost REAL,
    output_cost REAL,
    total_cost REAL,
    input_json TEXT,
    output_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (run_id, tool_name, idempotency_key, phase),
    FOREIGN KEY(run_id) REFERENCES agent_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_run_seq ON tool_calls (run_id, seq_no);
CREATE INDEX IF NOT EXISTS idx_tool_calls_parent ON tool_calls (parent_tool_call_id);
"""

SCHEMA_SQL_MYSQL = """
CREATE TABLE IF NOT EXISTS agent_runs (
    id CHAR(36) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    status VARCHAR(32) NOT NULL,
    tags JSON NULL,
    budget_limit DOUBLE NULL,
    total_prompt_tokens BIGINT NOT NULL DEFAULT 0,
    total_completion_tokens BIGINT NOT NULL DEFAULT 0,
    total_tokens BIGINT NOT NULL DEFAULT 0,
    total_cost DOUBLE NOT NULL DEFAULT 0,
    input_json JSON NULL,
    output_json JSON NULL,
    error TEXT NULL,
    replay_of CHAR(36) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

CREATE INDEX idx_agent_runs_name ON agent_runs (name);
CREATE INDEX idx_agent_runs_status ON agent_runs (status);

CREATE TABLE IF NOT EXISTS tool_calls (
    id CHAR(36) PRIMARY KEY,
    run_id CHAR(36) NOT NULL,
    seq_no INT NOT NULL,
    tool_name VARCHAR(255) NOT NULL,
    idempotency_key VARCHAR(64) NOT NULL,
    phase VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL,
    parent_tool_call_id CHAR(36) NULL,
    internal TINYINT(1) NOT NULL DEFAULT 0,
    provider VARCHAR(64) NULL,
    model VARCHAR(128) NULL,
    request_fingerprint VARCHAR(255) NULL,
    prompt_tokens BIGINT NULL,
    completion_tokens BIGINT NULL,
    total_tokens BIGINT NULL,
    input_cost DOUBLE NULL,
    output_cost DOUBLE NULL,
    total_cost DOUBLE NULL,
    input_json JSON NULL,
    output_json JSON NULL,
    error TEXT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_tool_call (run_id, tool_name, idempotency_key, phase),
    INDEX idx_tool_calls_run_seq (run_id, seq_no),
    INDEX idx_tool_calls_parent (parent_tool_call_id),
    CONSTRAINT fk_tool_calls_run_id FOREIGN KEY (run_id) REFERENCES agent_runs(id) ON DELETE CASCADE
);
"""


def get_schema_sql(dialect_name: str) -> str:
    d = (dialect_name or "").lower()
    if d.startswith("sqlite"):
        return SCHEMA_SQL_SQLITE
    if d.startswith("mysql"):
        return SCHEMA_SQL_MYSQL
    return SCHEMA_SQL_POSTGRES

SCHEMA_SQL = SCHEMA_SQL_SQLITE
