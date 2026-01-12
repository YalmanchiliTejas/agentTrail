from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from .schema import get_schema_sql

# --- Connection string helpers ---
#
# SQLite (default):
#   sqlite+pysqlite:////absolute/path/agenttrail.db
#   sqlite+pysqlite:///./agenttrail.db
#
# MySQL (PyMySQL):
#   mysql+pymysql://user:password@host:3306/db_name?charset=utf8mb4

DEFAULT_SQLITE_PATH = os.environ.get("AGENTTRAIL_SQLITE_PATH", "./agenttrail.db")
DEFAULT_SQLITE_CONN_STR = f"sqlite+pysqlite:///{DEFAULT_SQLITE_PATH}"

DEFAULT_MYSQL_CONN_STR = os.environ.get(
    "AGENTTRAIL_MYSQL_CONN_STR",
    "mysql+pymysql://agenttrail:agenttrail@127.0.0.1:3306/agenttrail?charset=utf8mb4",
)


def sqlite_connection_string(path: str = DEFAULT_SQLITE_PATH) -> str:
    # Accept either relative or absolute paths.
    if path.startswith("/"):
        return f"sqlite+pysqlite:////{path.lstrip('/')}"
    return f"sqlite+pysqlite:///{path}"


def mysql_connection_string(
    *,
    user: str = "agenttrail",
    password: str = "agenttrail",
    host: str = "127.0.0.1",
    port: int = 3306,
    database: str = "agenttrail",
    charset: str = "utf8mb4",
    driver: str = "pymysql",
) -> str:
    return (
        f"mysql+{driver}://{user}:{password}@{host}:{int(port)}/{database}?charset={charset}"
    )


@dataclass
class Database:
    engine: Engine

    @classmethod
    def from_connection_string(cls, conn_str: str) -> "Database":
        engine_kwargs: dict[str, Any] = {
            "future": True,
            "pool_pre_ping": True,
        }

        # SQLite defaults are a bit restrictive for multi-threaded apps.
        if conn_str.lower().startswith("sqlite"):
            engine_kwargs["connect_args"] = {"check_same_thread": False}

        engine = create_engine(conn_str, **engine_kwargs)
        db = cls(engine=engine)
        db.create_schema_if_needed()
        return db

    @classmethod
    def from_env(cls) -> "Database":
        # Preferred env var name for OSS/local:
        #   AGENTTRAIL_DB_URL
        conn_str = (
            os.environ.get("AGENTTRAIL_DB_URL")
            or os.environ.get("AGENTTRAIL_DATABASE_URL")
            or os.environ.get("DATABASE_URL")
        )

        if not conn_str:
            # Default to SQLite in the working directory.
            conn_str = DEFAULT_SQLITE_CONN_STR

        return cls.from_connection_string(conn_str)

    def create_schema_if_needed(self) -> None:
        schema_sql = get_schema_sql(self.engine.dialect.name)

        with self.engine.begin() as conn:
            # Enable FK enforcement on SQLite.
            if (self.engine.dialect.name or "").lower().startswith("sqlite"):
                conn.exec_driver_sql("PRAGMA foreign_keys = ON")

            for statement in schema_sql.split(";"):
                stmt = statement.strip()
                if not stmt:
                    continue
                try:
                    conn.execute(text(stmt))
                except Exception as e:
                    # Make schema creation idempotent across dialects.
                    # MySQL doesn't support "IF NOT EXISTS" for CREATE INDEX.
                    msg = str(e).lower()
                    if "duplicate key name" in msg:
                        continue
                    if "already exists" in msg and ("index" in msg or "table" in msg):
                        continue
                    raise

    def execute(self, sql: str, params: dict | None = None) -> None:
        with self.engine.begin() as conn:
            conn.execute(text(sql), params or {})

    def fetchone(self, sql: str, params: dict | None = None) -> Any:
        with self.engine.begin() as conn:
            result = conn.execute(text(sql), params or {})
            return result.fetchone()

    def fetchall(self, sql: str, params: dict | None = None) -> list[Any]:
        with self.engine.begin() as conn:
            result = conn.execute(text(sql), params or {})
            return list(result)
