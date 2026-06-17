"""Tests for the run history store and its JSONL outbox."""

import json

import pytest

import conductor.history as hist_mod


@pytest.fixture
def isolated_history(tmp_path, monkeypatch):
    """Redirect the history DB (and thus the colocated outbox) to a tmp dir."""
    db_path = tmp_path / ".conductor" / "history.db"
    monkeypatch.setattr(hist_mod, "DB_PATH", db_path)
    return {"db": db_path, "outbox": db_path.parent / "runs.jsonl"}


def _run(workflow="demo", status="success"):
    return {
        "workflow": workflow,
        "started_at": "2026-06-17T08:00:00",
        "completed_at": "2026-06-17T08:00:05",
        "duration_seconds": 5.0,
        "status": status,
        "steps": [{"id": "a", "status": "success"}],
    }


class TestOutbox:
    def test_record_run_appends_a_jsonl_line(self, isolated_history):
        run_id = hist_mod.record_run(_run())

        outbox = isolated_history["outbox"]
        assert outbox.exists()
        lines = outbox.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1

        record = json.loads(lines[0])
        assert record["id"] == run_id
        assert record["workflow"] == "demo"
        assert record["status"] == "success"
        # Full envelope is preserved under "result".
        assert record["result"]["steps"] == [{"id": "a", "status": "success"}]

    def test_outbox_is_append_only(self, isolated_history):
        hist_mod.record_run(_run(workflow="first"))
        hist_mod.record_run(_run(workflow="second"))
        hist_mod.record_run(_run(workflow="third"))

        lines = isolated_history["outbox"].read_text(encoding="utf-8").splitlines()
        assert [json.loads(line)["workflow"] for line in lines] == [
            "first",
            "second",
            "third",
        ]
        # ids are monotonically increasing alongside the SQLite rowids.
        ids = [json.loads(line)["id"] for line in lines]
        assert ids == sorted(ids)

    def test_each_line_is_independently_valid_json(self, isolated_history):
        hist_mod.record_run(_run(workflow="a"))
        hist_mod.record_run(_run(workflow="b", status="failed"))

        for line in isolated_history["outbox"].read_text().splitlines():
            json.loads(line)  # would raise if a line were not self-contained

    def test_outbox_and_sqlite_stay_in_sync(self, isolated_history):
        run_id = hist_mod.record_run(_run(workflow="sync"))

        from_db = hist_mod.get_run(run_id)
        from_outbox = hist_mod.read_outbox()[-1]["result"]
        assert from_db == from_outbox

    def test_outbox_failure_rolls_back_sqlite(self, isolated_history, monkeypatch):
        # If the outbox append fails, neither store should retain the run, so
        # SQLite/outbox parity holds rather than leaving an orphan DB row.
        def boom(*args, **kwargs):
            raise OSError("outbox unwritable")

        monkeypatch.setattr(hist_mod, "_append_outbox", boom)

        with pytest.raises(OSError):
            hist_mod.record_run(_run(workflow="doomed"))

        assert hist_mod.get_history() == []
        assert hist_mod.read_outbox() == []


class TestReadOutbox:
    def test_returns_empty_when_no_outbox(self, isolated_history):
        assert hist_mod.read_outbox() == []

    def test_returns_records_oldest_first(self, isolated_history):
        for name in ("one", "two", "three"):
            hist_mod.record_run(_run(workflow=name))

        records = hist_mod.read_outbox()
        assert [r["workflow"] for r in records] == ["one", "two", "three"]

    def test_limit_keeps_the_tail(self, isolated_history):
        for name in ("one", "two", "three"):
            hist_mod.record_run(_run(workflow=name))

        records = hist_mod.read_outbox(limit=2)
        assert [r["workflow"] for r in records] == ["two", "three"]

    def test_limit_zero_returns_nothing(self, isolated_history):
        for name in ("one", "two", "three"):
            hist_mod.record_run(_run(workflow=name))

        # limit=0 means "no records", consistent with get_history(0) — not the
        # whole list (which records[-0:] would have returned).
        assert hist_mod.read_outbox(limit=0) == []
