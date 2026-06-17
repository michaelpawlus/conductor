"""SQLite-backed workflow run history.

SQLite (``history.db``) is the queryable store behind ``conductor history`` and
the source of truth. Alongside it, every run is also appended to an append-only
JSONL *outbox* (``runs.jsonl``) in the same directory. The outbox is the agent-
and Obsidian-friendly handoff surface: conductor stays a data-gathering tool
that emits one JSON object per line, and downstream readers (a Claude Code
session or a scheduled trigger) consume the stream and synthesize it.

The two stores can't be written truly atomically, so SQLite leads: a run is
committed there first, then mirrored to the outbox best-effort. That guarantees
the outbox is never *ahead* of SQLite — every outbox id resolves via
``conductor history``. The outbox may lag (a failed append is logged, not
raised), but a lagging run is still durably recorded in ``history.db``.
"""

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

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

    SQLite is the source of truth: the row is committed first, then mirrored to
    the JSONL outbox best-effort. This keeps the outbox from ever getting
    *ahead* of SQLite — it can only lag, and a lagging run is still durably in
    ``history.db`` (and thus backfillable). A failed append is logged rather
    than raised, so it can neither roll back an already-durable run nor surface
    an outbox id that ``conductor history`` cannot resolve.
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

    try:
        _append_outbox(run_id, result)
    except Exception:
        logger.warning(
            "Run %s committed to history.db but outbox append failed; "
            "run is still queryable via `conductor history`.",
            run_id,
            exc_info=True,
        )
    return run_id


def _ends_without_newline(path: Path) -> bool:
    """True if the file exists, is non-empty, and its last byte is not ``\\n``.

    This only happens when a previous writer was killed mid-append and left an
    unterminated fragment; a live writer always finishes its line with ``\\n``.
    """
    try:
        if path.stat().st_size == 0:
            return False
    except OSError:
        return False
    with path.open("rb") as f:
        f.seek(-1, os.SEEK_END)
        return f.read(1) != b"\n"


def _append_outbox(run_id: int, result: dict[str, Any]) -> None:
    """Append one run to the JSONL outbox as a single line.

    Each line carries the run's identity at top level (so readers can skim
    without parsing ``result``) plus the full envelope under ``result``. The
    SQLite row id ties the line back to ``conductor history``.

    The append is plain and lock-free. We deliberately do *not* truncate on a
    failed/partial write: multiple conductor processes can append to the same
    file concurrently, and rewinding to a pre-write offset could drop a line a
    different process appended in the meantime. Instead, :func:`read_outbox`
    tolerates a malformed trailing fragment by skipping it — and any run that
    fails to reach the outbox is still durably in ``history.db``.

    If the file ends with an unterminated fragment (a prior writer killed
    mid-append), we prepend a newline so this record lands on its own line
    rather than being merged into — and skipped along with — that bad tail.
    The fragment is then isolated on its own line and skipped by itself, so a
    corrupt tail costs at most the one interrupted run, never the next good
    one. A spurious blank line is harmless: ``read_outbox`` skips empties.
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
    prefix = "\n" if _ends_without_newline(path) else ""
    with path.open("a", encoding="utf-8") as f:
        f.write(prefix + json.dumps(record) + "\n")


def read_outbox(limit: int | None = None) -> list[dict[str, Any]]:
    """Read run records from the JSONL outbox, ordered oldest to newest by id.

    Returns an empty list if the outbox does not exist yet. ``limit`` keeps only
    the most recent N records (the tail), which is what a synthesis reader
    typically wants; ``limit=0`` returns no records (matching ``get_history(0)``),
    and ``None`` returns everything.

    Records are sorted by run id rather than trusting file order: the append
    happens after the SQLite commit and outside any lock, so concurrent runs can
    land their lines out of id-order. Sorting by id (the monotonic source of
    truth) makes the tail genuinely the most recent runs regardless of write
    interleaving.

    Malformed lines are skipped (and logged) rather than raised: a process
    killed mid-append can leave a truncated fragment that no writer-side handler
    got to clean up, and one bad line must not break every read.
    """
    path = _outbox_path()
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning(
                    "Skipping malformed line in outbox %s: %r", path, line[:120]
                )
    records.sort(key=lambda r: r["id"])
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
