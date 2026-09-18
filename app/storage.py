"""SQLite persistence for simulation runs (stdlib sqlite3, no ORM)."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('completed', 'failed')),
    name         TEXT,
    request_json TEXT NOT NULL,
    result_json  TEXT,
    errors_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs (created_at DESC);
"""


class RunStore:
    """Thread-safe store of simulation records."""

    def __init__(self, db_path: str | Path):
        self._db_path = str(db_path)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def save_run(self, record: dict[str, Any]) -> None:
        """Insert a run record. ``request``/``result``/``errors`` are JSON-serialisable."""
        with self._write_lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (run_id, created_at, status, name,
                                  request_json, result_json, errors_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["run_id"],
                    record["created_at"],
                    record["status"],
                    record.get("name"),
                    json.dumps(record["request"], ensure_ascii=False),
                    json.dumps(record["result"], ensure_ascii=False) if record.get("result") is not None else None,
                    json.dumps(record["errors"], ensure_ascii=False) if record.get("errors") is not None else None,
                ),
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._to_record(row) if row else None

    def list_runs(self, limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
        """Return (summaries, total_count), newest first."""
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
            rows = conn.execute(
                """
                SELECT run_id, created_at, status, name, request_json
                FROM runs
                ORDER BY created_at DESC, rowid DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        summaries = []
        for row in rows:
            request = json.loads(row["request_json"])
            summaries.append(
                {
                    "run_id": row["run_id"],
                    "status": row["status"],
                    "name": row["name"],
                    "created_at": row["created_at"],
                    "sample_count": len(request.get("samples", [])),
                    "transfer_count": len(request.get("transfers", [])),
                }
            )
        return summaries, total

    @staticmethod
    def _to_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "run_id": row["run_id"],
            "status": row["status"],
            "name": row["name"],
            "created_at": row["created_at"],
            "request": json.loads(row["request_json"]),
            "result": json.loads(row["result_json"]) if row["result_json"] is not None else None,
            "errors": json.loads(row["errors_json"]) if row["errors_json"] is not None else None,
        }
