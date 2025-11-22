
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from .schema import SCHEMA_SQL


@dataclass
class Database:
    engine: Engine

    @classmethod
    def from_connection_string(cls, conn_str: str) -> "Database":
        engine = create_engine(conn_str, future=True)
        db = cls(engine=engine)
        db.create_schema_if_needed()
        return db

    def create_schema_if_needed(self) -> None:
        # Very simple migration: just run the schema as "IF NOT EXISTS"
        with self.engine.begin() as conn:
            for statement in SCHEMA_SQL.split(";"):
                stmt = statement.strip()
                if not stmt:
                    continue
                conn.execute(text(stmt))

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