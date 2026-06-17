"""SQLite-backed workflow run history.

SQLite (``history.db``) is the queryable store behind ``conductor history``.
Alongside it, every run is also appended to an append-only JSONL *outbox*
(``runs.jsonl``) in the same directory. The outbox is the agent- and
Obsidian-friendly handoff surface: conductor stays a data-gathering tool that
emits one JSON object per line, and downstream readers (a Claude Code session
or a scheduled trigger) consume the stream and synthesize it. The outbox does
not replace SQLite — both are written from the same :func:`record_run`.
"""

import json
import sqlite3
from pathlib import Path
from typing import Any

DB_PATH = Path.home() / ".conductor" / "history.db"


def _outbox_path() -> Path:
    """The JSONL outbox lives beside the history DB.

    Derived from ``DB_PATH`` at call time so redirecting the DB (e.g. in tests)
    keeps the outbox colocated automatically.
    """
    return DB_PATH.parent / "runs.jsonl"


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
    """Record a workflow run. Returns the row ID.

    Writes to both stores: the SQLite ``runs`` table and the JSONL outbox.
    """
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
        run_id: int = cursor.lastrowid  # type: ignore[assignment]
    finally:
        conn.close()

    _append_outbox(run_id, result)
    return run_id


def _append_outbox(run_id: int, result: dict[str, Any]) -> None:
    """Append one run to the JSONL outbox as a single line.

    Each line carries the run's identity at top level (so readers can skim
    without parsing ``result``) plus the full envelope under ``result``. The
    SQLite row id ties the line back to ``conductor history``.
    """
    record = {
        "id": run_id,
        "workflow": result["workflow"],
        "status": result["status"],
        "started_at": result["started_at"],
        "completed_at": result.get("completed_at"),
        "duration_seconds": result.get("duration_seconds"),
        "result": result,
    }
    path = _outbox_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def read_outbox(limit: int | None = None) -> list[dict[str, Any]]:
    """Read run records from the JSONL outbox, newest last.

    Returns an empty list if the outbox does not exist yet. ``limit`` keeps only
    the most recent N records (the tail), which is what a synthesis reader
    typically wants; ``limit=0`` returns no records (matching ``get_history(0)``),
    and ``None`` returns everything.
    """
    path = _outbox_path()
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    if limit is not None:
        if limit <= 0:
            return []
        return records[-limit:]
    return records


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
