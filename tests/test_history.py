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

    def test_outbox_failure_keeps_sqlite_row(self, isolated_history, monkeypatch):
        # SQLite leads: a failed outbox append must not lose the committed run
        # and must not raise (history is best-effort for callers). The outbox
        # simply lags — it never gets ahead with an orphan/reusable id.
        def boom(*args, **kwargs):
            raise OSError("outbox unwritable")

        monkeypatch.setattr(hist_mod, "_append_outbox", boom)

        run_id = hist_mod.record_run(_run(workflow="kept"))

        # Durably recorded in SQLite...
        assert hist_mod.get_run(run_id) is not None
        assert [r["workflow"] for r in hist_mod.get_history()] == ["kept"]
        # ...with no outbox line ahead of it.
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

    def test_skips_malformed_lines(self, isolated_history):
        # A truncated fragment (e.g. a process killed mid-append) must not break
        # every read — the reader skips the bad line and returns the good ones.
        outbox = isolated_history["outbox"]
        outbox.parent.mkdir(parents=True, exist_ok=True)
        good1 = json.dumps({"id": 1, "workflow": "first", "result": {"id": 1}})
        good2 = json.dumps({"id": 2, "workflow": "second", "result": {"id": 2}})
        outbox.write_text(
            good1 + "\n" + '{"id": 3, "workflow": "trunc' + "\n" + good2 + "\n",
            encoding="utf-8",
        )

        assert [r["workflow"] for r in hist_mod.read_outbox()] == ["first", "second"]

    def test_orders_by_id_not_file_order(self, isolated_history):
        # Concurrent runs can append out of order (a lower-id run preempted
        # until after a higher-id one). read_outbox sorts by id so the tail is
        # genuinely the most recent run, not whatever line landed last.
        outbox = isolated_history["outbox"]
        outbox.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps({"id": 2, "workflow": "second", "result": {"id": 2}}),
            json.dumps({"id": 1, "workflow": "first", "result": {"id": 1}}),
        ]
        outbox.write_text("\n".join(lines) + "\n", encoding="utf-8")

        assert [r["workflow"] for r in hist_mod.read_outbox()] == ["first", "second"]
        assert [r["workflow"] for r in hist_mod.read_outbox(limit=1)] == ["second"]
