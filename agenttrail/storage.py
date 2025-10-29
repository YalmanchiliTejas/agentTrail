"""
agenttrail/storage.py

Persistence layer using SQLite for the demo (easy to ship, zero external deps).
- WAL mode allows multiple readers and a writer with fewer locks.
- Schema is append-friendly and mirrors a Temporal-like mental model:
  * runs        : one row per workflow run
  * steps       : one row per step attempt
  * tool_calls  : capture tool I/O for deterministic replay (stubbed in demo)
  * events      : append-only audit/event log (human/time-series friendly)
  * outbox      : for exactly-once side-effect delivery (not used in the demo)

For production: switch to Postgres with the same schema. WAL and busy timeouts
are left here to make concurrency tolerable even in SQLite demos.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime
import json
import threading

# DDL statements. We execute each statement individually for better error reporting.
SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS runs(
  run_id TEXT PRIMARY KEY,
  workflow TEXT NOT NULL,
  status TEXT NOT NULL,              -- running|completed|failed|compensating|compensated
  started_at TEXT NOT NULL,          -- ISO8601 UTC timestamps for easy JSON interchange
  ended_at TEXT,
  determinism_hash TEXT,
  root_trace_id TEXT
);
CREATE TABLE IF NOT EXISTS steps(
  step_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  name TEXT NOT NULL,                -- step function name (logical step id)
  attempt INTEGER NOT NULL,          -- attempt count (1-based)
  status TEXT NOT NULL,              -- started|completed|failed|compensated
  started_at TEXT NOT NULL,
  ended_at TEXT,
  trace_id TEXT,                     -- for OpenTelemetry correlation
  span_id TEXT,
  idem_key TEXT,                     -- idempotency key for this call
  UNIQUE(run_id, name, attempt)
);
CREATE INDEX IF NOT EXISTS idx_steps_run ON steps(run_id);
CREATE INDEX IF NOT EXISTS idx_steps_idem ON steps(idem_key);

CREATE TABLE IF NOT EXISTS tool_calls(
  tool_call_id TEXT PRIMARY KEY,
  step_id TEXT NOT NULL,             -- which step produced this call
  tool TEXT NOT NULL,                -- tool name (e.g., "http", "sql", "openai")
  request_json TEXT NOT NULL,        -- serialized request (for replay)
  response_json TEXT,                -- serialized response (available when successful)
  error TEXT,                        -- error string if failed
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events(
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  step_id TEXT,                      -- may be NULL for run-level events
  kind TEXT NOT NULL,                -- e.g., RUN_STARTED, STEP_COMPLETED, CACHE_HIT
  at TEXT NOT NULL,
  data_json TEXT                     -- free-form JSON payload for diagnostics
);

CREATE TABLE IF NOT EXISTS outbox(
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  step_id TEXT NOT NULL,
  topic TEXT NOT NULL,               -- where to deliver (queue topic, webhook URL name, etc.)
  payload_json TEXT NOT NULL,        -- message to deliver
  delivered INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
"""

class DB:
    """
    Thin wrapper around a per-thread SQLite connection with small helper methods.
    We expose coarse operations used by the runtime; raw SQL lives here to keep the
    runtime logic readable.
    """
    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        self._init()

    # -- Connection plumbing --
    def _connect(self):
        # isolation_level=None => autocommit; we explicitly manage BEGIN/COMMIT in tx()
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=5000")      # retry when DB is locked
        conn.execute("PRAGMA journal_mode=WAL")       # allow readers during writes
        conn.row_factory = sqlite3.Row
        return conn

    def _get_conn(self):
        if not hasattr(self._local, "conn"):
            self._local.conn = self._connect()
        return self._local.conn

    def _init(self):
        c = self._get_conn()
        # Execute each statement separately (split on ";
        #") for clarity.
        for stmt in SCHEMA.strip().split(";\n"):
            s = stmt.strip()
            if s:
                c.execute(s)

    @contextmanager
    def tx(self):
        """
        Transaction context manager. Ensures BEGIN/COMMIT/ROLLBACK boundaries.
        Use this to keep step state changes and event appends atomic.
        """
        conn = self._get_conn()
        try:
            conn.execute("BEGIN")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # -- CRUD helpers for runtime --
    def insert_run(self, run_id, workflow, status, started_at, determinism_hash=None, root_trace_id=None):
        with self.tx() as c:
            c.execute(
                "INSERT INTO runs(run_id, workflow, status, started_at, determinism_hash, root_trace_id) VALUES(?,?,?,?,?,?)",
                (run_id, workflow, status, started_at, determinism_hash, root_trace_id),
            )

    def update_run_status(self, run_id, status, ended_at=None):
        with self.tx() as c:
            c.execute("UPDATE runs SET status=?, ended_at=? WHERE run_id=?", (status, ended_at, run_id))

    def insert_step(self, step_id, run_id, name, attempt, status, started_at, idem_key, trace_id=None, span_id=None):
        with self.tx() as c:
            c.execute(
                "INSERT INTO steps(step_id, run_id, name, attempt, status, started_at, idem_key, trace_id, span_id) VALUES(?,?,?,?,?,?,?,?,?)",
                (step_id, run_id, name, attempt, status, started_at, idem_key, trace_id, span_id),
            )

    def update_step(self, step_id, status, ended_at=None):
        with self.tx() as c:
            c.execute("UPDATE steps SET status=?, ended_at=? WHERE step_id=?", (status, ended_at, step_id))

    def complete_tool_call(self, tool_call_id, response_json=None, error=None):
        with self.tx() as c:
            c.execute("UPDATE tool_calls SET response_json=?, error=? WHERE tool_call_id=?", (response_json, error, tool_call_id))

    def log_event(self, run_id, kind, data=None, step_id=None):
        """
        Append an event. We keep 'data' as compact JSON for easy ingestion into logs/metrics.
        """
        with self.tx() as c:
            c.execute(
                "INSERT INTO events(run_id, step_id, kind, at, data_json) VALUES(?,?,?,?,?)",
                (run_id, step_id, kind, datetime.utcnow().isoformat(), json.dumps(data or {}, separators=(',',':'))),
            )

    def get_step_by_idem(self, idem_key):
        """
        Fetch the most recent step that already completed (or was compensated) with this idempotency key.
        Used to return cached results or to drive deterministic replay.
        """
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT * FROM steps WHERE idem_key=? AND status IN ('completed','compensated') ORDER BY attempt DESC LIMIT 1",
            (idem_key,),
        )
        return cur.fetchone()

    def insert_tool_call(self, tool_call_id, step_id, tool, request_json):
        with self.tx() as c:
            c.execute(
                "INSERT INTO tool_calls(tool_call_id, step_id, tool, request_json, created_at) VALUES(?,?,?,?,?)",
                (tool_call_id, step_id, tool, request_json, datetime.utcnow().isoformat()),
            )

    def pending_steps(self, run_id):
        """
        Return completed steps (most recent first). Compensation walks this list to undo side-effects.
        """
        conn = self._get_conn()
        cur = conn.execute("SELECT * FROM steps WHERE run_id=? AND status='completed' ORDER BY ended_at DESC", (run_id,))
        return cur.fetchall()

    def run_summary(self, run_id):
        """
        Useful for the dashboard: a quick at-a-glance summary.
        """
        conn = self._get_conn()
        run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        steps = conn.execute("SELECT * FROM steps WHERE run_id=? ORDER BY started_at", (run_id,)).fetchall()
        events = conn.execute("SELECT kind, COUNT(*) as n FROM events WHERE run_id=? GROUP BY kind", (run_id,)).fetchall()
        return run, steps, events
