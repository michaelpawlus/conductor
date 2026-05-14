"""Tests for the pure `diff_reports` function and `load_baseline_report` helper."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conductor.doctor import (
    DOCTOR_SCHEMA_VERSION,
    diff_reports,
    load_baseline_report,
)


def _project(
    name: str, status: str, *, checks: list[dict] | None = None, path: str = "/p"
) -> dict:
    return {
        "name": name,
        "path": path,
        "status": status,
        "checks": checks or [{"id": "path-exists", "status": "ok"}],
    }


def _report(projects: list[dict], *, checked_at: str = "2026-05-13T12:00:00Z") -> dict:
    return {
        "checked_at": checked_at,
        "registry_path": "/fake/registry.yml",
        "summary": {
            "projects": len(projects),
            "ok": sum(1 for p in projects if p["status"] == "ok"),
            "warnings": sum(1 for p in projects if p["status"] == "warning"),
            "errors": sum(1 for p in projects if p["status"] == "error"),
        },
        "projects": projects,
    }


class TestDiffReports:
    def test_identical_reports_produce_empty_diff(self):
        prev = _report([_project("a", "ok"), _project("b", "ok")])
        curr = _report([_project("a", "ok"), _project("b", "ok")])
        diff = diff_reports(prev, curr)
        assert diff["regressed"] == []
        assert diff["improved"] == []
        assert diff["newly_added"] == []
        assert diff["removed"] == []
        assert diff["vs_baseline"] == "2026-05-13T12:00:00Z"

    def test_regression_ok_to_warning(self):
        prev = _report([_project("alpha", "ok")])
        curr = _report([_project("alpha", "warning")])
        diff = diff_reports(prev, curr)
        assert diff["regressed"] == [{"name": "alpha", "from": "ok", "to": "warning"}]
        assert diff["improved"] == []

    def test_regression_warning_to_error(self):
        prev = _report([_project("alpha", "warning")])
        curr = _report([_project("alpha", "error")])
        diff = diff_reports(prev, curr)
        assert diff["regressed"][0]["from"] == "warning"
        assert diff["regressed"][0]["to"] == "error"

    def test_regression_includes_failed_check_id(self):
        prev = _report(
            [
                _project(
                    "alpha",
                    "ok",
                    checks=[
                        {"id": "path-exists", "status": "ok"},
                        {"id": "help-parses", "status": "ok"},
                    ],
                )
            ]
        )
        curr = _report(
            [
                _project(
                    "alpha",
                    "warning",
                    checks=[
                        {"id": "path-exists", "status": "ok"},
                        {"id": "help-parses", "status": "warning", "detail": "..."},
                    ],
                )
            ]
        )
        diff = diff_reports(prev, curr)
        assert diff["regressed"][0]["failed_check"] == "help-parses"

    def test_improvement_warning_to_ok(self):
        prev = _report([_project("alpha", "warning")])
        curr = _report([_project("alpha", "ok")])
        diff = diff_reports(prev, curr)
        assert diff["improved"] == [{"name": "alpha", "from": "warning", "to": "ok"}]
        assert diff["regressed"] == []

    def test_improvement_error_to_warning(self):
        prev = _report([_project("alpha", "error")])
        curr = _report([_project("alpha", "warning")])
        diff = diff_reports(prev, curr)
        assert diff["improved"][0]["from"] == "error"
        assert diff["improved"][0]["to"] == "warning"

    def test_newly_added_project(self):
        prev = _report([_project("alpha", "ok")])
        curr = _report([_project("alpha", "ok"), _project("beta", "warning")])
        diff = diff_reports(prev, curr)
        assert diff["newly_added"] == [{"name": "beta", "status": "warning"}]
        assert diff["removed"] == []

    def test_removed_project_carries_last_status(self):
        prev = _report([_project("alpha", "ok"), _project("beta", "error")])
        curr = _report([_project("alpha", "ok")])
        diff = diff_reports(prev, curr)
        assert diff["removed"] == [{"name": "beta", "last_status": "error"}]
        assert diff["newly_added"] == []

    def test_mixed_diff(self):
        prev = _report(
            [
                _project("stable", "ok"),
                _project("fixme", "warning"),
                _project("gone", "ok"),
            ]
        )
        curr = _report(
            [
                _project("stable", "ok"),
                _project("fixme", "ok"),
                _project("new", "warning"),
                _project("oops", "error"),
            ]
        )
        # Force "oops" through as a regression of a previously-warning project:
        prev["projects"].append(_project("oops", "warning"))

        diff = diff_reports(prev, curr)
        assert {p["name"] for p in diff["regressed"]} == {"oops"}
        assert {p["name"] for p in diff["improved"]} == {"fixme"}
        assert {p["name"] for p in diff["newly_added"]} == {"new"}
        assert {p["name"] for p in diff["removed"]} == {"gone"}

    def test_vs_baseline_prefers_generated_at_over_checked_at(self):
        prev = {
            "generated_at": "2026-05-10T00:00:00Z",
            "checked_at": "2026-05-09T00:00:00Z",
            "projects": [_project("alpha", "ok")],
        }
        curr = _report([_project("alpha", "ok")])
        diff = diff_reports(prev, curr)
        assert diff["vs_baseline"] == "2026-05-10T00:00:00Z"


class TestLoadBaselineReport:
    def test_loads_plain_doctor_json(self, tmp_path: Path):
        f = tmp_path / "baseline.json"
        payload = _report([_project("alpha", "ok")])
        f.write_text(json.dumps(payload))
        baseline = load_baseline_report(f)
        assert baseline["projects"][0]["name"] == "alpha"

    def test_strips_diff_field_from_prior_diff_output(self, tmp_path: Path):
        f = tmp_path / "baseline.json"
        payload = {
            "schema_version": DOCTOR_SCHEMA_VERSION,
            "checked_at": "2026-05-13T12:00:00Z",
            "projects": [_project("alpha", "ok")],
            "diff": {
                "vs_baseline": "2026-05-12T12:00:00Z",
                "regressed": [],
                "improved": [],
                "newly_added": [],
                "removed": [],
            },
        }
        f.write_text(json.dumps(payload))
        baseline = load_baseline_report(f)
        assert "diff" not in baseline
        assert baseline["projects"][0]["name"] == "alpha"

    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="cannot read"):
            load_baseline_report(tmp_path / "nope.json")

    def test_malformed_json_raises(self, tmp_path: Path):
        f = tmp_path / "bad.json"
        f.write_text("{ not valid json")
        with pytest.raises(ValueError, match="not valid JSON"):
            load_baseline_report(f)

    def test_schema_version_mismatch_raises(self, tmp_path: Path):
        f = tmp_path / "future.json"
        f.write_text(
            json.dumps(
                {
                    "schema_version": 999,
                    "checked_at": "2099-01-01T00:00:00Z",
                    "projects": [],
                }
            )
        )
        with pytest.raises(ValueError, match="schema_version"):
            load_baseline_report(f)

    def test_missing_projects_field_raises(self, tmp_path: Path):
        f = tmp_path / "weird.json"
        f.write_text(json.dumps({"checked_at": "x"}))
        with pytest.raises(ValueError, match="projects"):
            load_baseline_report(f)

    def test_non_object_payload_raises(self, tmp_path: Path):
        f = tmp_path / "arr.json"
        f.write_text(json.dumps([1, 2, 3]))
        with pytest.raises(ValueError, match="JSON object"):
            load_baseline_report(f)
