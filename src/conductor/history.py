"""SQLite-backed workflow run history."""

import json
import sqlite3
from pathlib import Path
from typing import Any

DB_PATH = Path.home() / ".conductor" / "history.db"


def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            duration_seconds REAL,
            status TEXT NOT NULL,
            result_json TEXT
        )
    """)
    conn.commit()
    return conn


def record_run(result: dict[str, Any]) -> int:
    """Record a workflow run. Returns the row ID."""
    conn = _get_conn()
    try:
        cursor = conn.execute(
            """INSERT INTO runs
               (workflow, started_at, completed_at, duration_seconds, status, result_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                result["workflow"],
                result["started_at"],
                result.get("completed_at"),
                result.get("duration_seconds"),
                result["status"],
                json.dumps(result),
            ),
        )
        conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]
    finally:
        conn.close()


def get_history(limit: int = 20) -> list[dict[str, Any]]:
    """Retrieve recent workflow runs."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT id, workflow, started_at, completed_at, duration_seconds, status "
            "FROM runs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_run(run_id: int) -> dict[str, Any] | None:
    """Retrieve the full result of a specific run."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT result_json FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row:
            return json.loads(row["result_json"])
        return None
    finally:
        conn.close()
