"""Tests for conductor.doctor."""

from __future__ import annotations

from pathlib import Path

import pytest

from conductor.doctor import run_doctor


def _make_fake_cli(
    project_dir: Path, *, cli_name: str, help_text: str, exit_code: int = 0
) -> dict[str, str | int]:
    """Create a fake .venv/bin/<cli_name> script that prints `help_text` on --help."""
    bin_dir = project_dir / ".venv" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / cli_name
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"HELP = {help_text!r}\n"
        f"EXIT = {exit_code}\n"
        "if len(sys.argv) > 1 and sys.argv[1] == '--help':\n"
        "    sys.stdout.write(HELP)\n"
        "    sys.exit(EXIT)\n"
        "sys.exit(2)\n"
    )
    script.chmod(0o755)
    return {
        "path": str(project_dir),
        "cli": cli_name,
        "runtime": "python",
        "venv": f".venv/bin/{cli_name}",
    }


RICH_HELP_7 = (
    "\n Usage: foo [OPTIONS]\n\n"
    "╭─ Commands ─────────────╮\n"
    "│ a  do a                │\n"
    "│ b  do b                │\n"
    "│ c  do c                │\n"
    "│ d  do d                │\n"
    "│ e  do e                │\n"
    "│ f  do f                │\n"
    "│ g  do g                │\n"
    "╰────────────────────────╯\n"
)


class TestRunDoctor:
    def test_empty_registry_summary(self):
        report = run_doctor({})
        assert report["summary"] == {"projects": 0, "ok": 0, "warnings": 0, "errors": 0}
        assert report["projects"] == []

    def test_missing_path_is_error(self, tmp_path: Path):
        registry = {
            "ghost": {
                "path": str(tmp_path / "does-not-exist"),
                "cli": "ghost",
                "runtime": "python",
                "commands_discovered": 0,
            }
        }
        report = run_doctor(registry)
        assert report["summary"]["errors"] == 1
        assert report["projects"][0]["status"] == "error"
        path_check = next(
            c for c in report["projects"][0]["checks"] if c["id"] == "path-exists"
        )
        assert path_check["status"] == "error"

    def test_commands_discovered_drift_warns(self, tmp_path: Path):
        proj = tmp_path / "drift"
        proj.mkdir()
        entry = _make_fake_cli(proj, cli_name="drift", help_text=RICH_HELP_7)
        entry["commands_discovered"] = 1  # registry says 1, live shows 7

        report = run_doctor({"drift": entry})
        project_report = report["projects"][0]
        cmd_check = next(
            c for c in project_report["checks"] if c["id"] == "commands-discovered"
        )
        assert cmd_check["status"] == "warning"
        assert cmd_check["live_count"] == 7
        assert cmd_check["registry_count"] == 1
        assert project_report["status"] == "warning"

    def test_commands_discovered_match_is_ok(self, tmp_path: Path):
        proj = tmp_path / "match"
        proj.mkdir()
        entry = _make_fake_cli(proj, cli_name="match", help_text=RICH_HELP_7)
        entry["commands_discovered"] = 7

        report = run_doctor({"match": entry})
        project_report = report["projects"][0]
        assert project_report["status"] == "ok"

    def test_check_json_adds_check_when_requested(self, tmp_path: Path):
        proj = tmp_path / "nojson"
        proj.mkdir()
        entry = _make_fake_cli(proj, cli_name="nojson", help_text=RICH_HELP_7)
        entry["commands_discovered"] = 7

        without = run_doctor({"nojson": entry})
        assert all(
            c["id"] != "json-flag-advertised" for c in without["projects"][0]["checks"]
        )

        with_json = run_doctor({"nojson": entry}, check_json=True)
        json_check = next(
            c
            for c in with_json["projects"][0]["checks"]
            if c["id"] == "json-flag-advertised"
        )
        assert json_check["status"] == "info"  # help text has no --json

    def test_check_json_ok_when_present(self, tmp_path: Path):
        proj = tmp_path / "hasjson"
        proj.mkdir()
        help_with_json = RICH_HELP_7.replace("│ a  do a", "│ a  do a --json")
        entry = _make_fake_cli(proj, cli_name="hasjson", help_text=help_with_json)
        entry["commands_discovered"] = 7

        report = run_doctor({"hasjson": entry}, check_json=True)
        json_check = next(
            c
            for c in report["projects"][0]["checks"]
            if c["id"] == "json-flag-advertised"
        )
        assert json_check["status"] == "ok"

    def test_project_filter_restricts_output(self, tmp_path: Path):
        proj = tmp_path / "filtered"
        proj.mkdir()
        entry = _make_fake_cli(proj, cli_name="filtered", help_text=RICH_HELP_7)
        entry["commands_discovered"] = 7

        registry = {
            "filtered": entry,
            "ghost": {
                "path": str(tmp_path / "missing"),
                "cli": "ghost",
                "runtime": "python",
                "commands_discovered": 0,
            },
        }
        report = run_doctor(registry, project_filter="filtered")
        assert len(report["projects"]) == 1
        assert report["projects"][0]["name"] == "filtered"

    def test_node_runtime_skips_commands_discovered(self, tmp_path: Path):
        proj = tmp_path / "node"
        proj.mkdir()
        entry = _make_fake_cli(proj, cli_name="node-cli", help_text=RICH_HELP_7)
        entry["commands_discovered"] = 0
        entry["runtime"] = "node"

        report = run_doctor({"node-cli": entry})
        ids = {c["id"] for c in report["projects"][0]["checks"]}
        assert "commands-discovered" not in ids

    def test_help_exit_nonzero_is_error(self, tmp_path: Path):
        proj = tmp_path / "broken"
        proj.mkdir()
        entry = _make_fake_cli(
            proj, cli_name="broken", help_text="boom", exit_code=1
        )
        entry["commands_discovered"] = 0

        report = run_doctor({"broken": entry})
        help_check = next(
            c for c in report["projects"][0]["checks"] if c["id"] == "help-parses"
        )
        assert help_check["status"] == "error"
        assert report["projects"][0]["status"] == "error"

    def test_venv_missing_warns(self, tmp_path: Path):
        proj = tmp_path / "novenv"
        proj.mkdir()
        registry = {
            "novenv": {
                "path": str(proj),
                "cli": "novenv",
                "runtime": "python",
                "venv": ".venv/bin/novenv",
                "commands_discovered": 0,
            }
        }
        report = run_doctor(registry)
        venv_check = next(
            (c for c in report["projects"][0]["checks"] if c["id"] == "venv-present"),
            None,
        )
        assert venv_check is not None
        assert venv_check["status"] == "warning"


def _stub_typer_duo(tmp_path: Path, payload_by_path: dict[str, str]) -> Path:
    """Write a fake `typer-duo` script that prints a canned JSON for each path arg.

    `payload_by_path` maps the absolute path arg → JSON string to emit on stdout.
    The "*" key acts as a fallback for any unmatched path.
    """
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "typer-duo"
    import json as _json

    encoded = _json.dumps(payload_by_path)
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json\n"
        f"PAYLOADS = {encoded!r}\n"
        "table = json.loads(PAYLOADS)\n"
        "args = sys.argv[1:]\n"
        # Expect: audit <path> --json
        "if len(args) < 2 or args[0] != 'audit':\n"
        "    sys.stderr.write('unexpected args')\n"
        "    sys.exit(2)\n"
        "path = args[1]\n"
        "out = table.get(path, table.get('*'))\n"
        "if out is None:\n"
        "    sys.stderr.write('no stub for path')\n"
        "    sys.exit(2)\n"
        "sys.stdout.write(out)\n"
        "sys.exit(0)\n"
    )
    script.chmod(0o755)
    return bin_dir


_AUDIT_OK = (
    '{"summary": {"commands_total": 3, "commands_with_json": 3, "score": 100, '
    '"severity_max": null}, "findings": []}'
)
_AUDIT_WARNING = (
    '{"summary": {"commands_total": 3, "commands_with_json": 3, "score": 100, '
    '"severity_max": "warning"}, "findings": [{"severity": "warning", "id": "x"}]}'
)
_AUDIT_ERROR = (
    '{"summary": {"commands_total": 3, "commands_with_json": 0, "score": 0, '
    '"severity_max": "error"}, "findings": [{"severity": "error", "id": "y"}]}'
)
_AUDIT_NO_ENTRY = '{"error": "no Typer entry point detected", "code": 2}'


class TestSubcommandsAudit:
    def test_ok_when_audit_clean(self, tmp_path: Path, monkeypatch):
        proj = tmp_path / "clean"
        proj.mkdir()
        entry = _make_fake_cli(proj, cli_name="clean", help_text=RICH_HELP_7)
        entry["commands_discovered"] = 7

        bin_dir = _stub_typer_duo(tmp_path, {str(proj): _AUDIT_OK})
        monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ['PATH']}")

        report = run_doctor({"clean": entry}, check_subcommands=True)
        check = next(
            c for c in report["projects"][0]["checks"] if c["id"] == "subcommands-audit"
        )
        assert check["status"] == "ok"
        assert check["score"] == 100
        assert check["findings_count"] == 0
        assert report["typer_duo_available"] is True

    def test_warning_when_audit_has_warnings(self, tmp_path: Path, monkeypatch):
        proj = tmp_path / "warn"
        proj.mkdir()
        entry = _make_fake_cli(proj, cli_name="warn", help_text=RICH_HELP_7)
        entry["commands_discovered"] = 7

        bin_dir = _stub_typer_duo(tmp_path, {str(proj): _AUDIT_WARNING})
        monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ['PATH']}")

        report = run_doctor({"warn": entry}, check_subcommands=True)
        check = next(
            c for c in report["projects"][0]["checks"] if c["id"] == "subcommands-audit"
        )
        assert check["status"] == "warning"
        assert check["findings_count"] == 1
        assert report["projects"][0]["status"] == "warning"

    def test_error_when_audit_has_errors(self, tmp_path: Path, monkeypatch):
        proj = tmp_path / "fail"
        proj.mkdir()
        entry = _make_fake_cli(proj, cli_name="fail", help_text=RICH_HELP_7)
        entry["commands_discovered"] = 7

        bin_dir = _stub_typer_duo(tmp_path, {str(proj): _AUDIT_ERROR})
        monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ['PATH']}")

        report = run_doctor({"fail": entry}, check_subcommands=True)
        check = next(
            c for c in report["projects"][0]["checks"] if c["id"] == "subcommands-audit"
        )
        assert check["status"] == "error"
        assert check["score"] == 0
        assert report["projects"][0]["status"] == "error"
        assert report["summary"]["errors"] == 1

    def test_info_when_typer_duo_reports_no_entry(self, tmp_path: Path, monkeypatch):
        proj = tmp_path / "noentry"
        proj.mkdir()
        entry = _make_fake_cli(proj, cli_name="noentry", help_text=RICH_HELP_7)
        entry["commands_discovered"] = 7

        bin_dir = _stub_typer_duo(tmp_path, {str(proj): _AUDIT_NO_ENTRY})
        monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ['PATH']}")

        report = run_doctor({"noentry": entry}, check_subcommands=True)
        check = next(
            c for c in report["projects"][0]["checks"] if c["id"] == "subcommands-audit"
        )
        assert check["status"] == "info"
        assert "no Typer entry point" in check["detail"]
        # info must not bubble up to error/warning rollup
        assert report["projects"][0]["status"] == "ok"

    def test_missing_typer_duo_marks_unavailable(self, tmp_path: Path, monkeypatch):
        proj = tmp_path / "nodup"
        proj.mkdir()
        entry = _make_fake_cli(proj, cli_name="nodup", help_text=RICH_HELP_7)
        entry["commands_discovered"] = 7

        # Wipe PATH so typer-duo cannot be found.
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))

        report = run_doctor({"nodup": entry}, check_subcommands=True)
        assert report["typer_duo_available"] is False
        assert "typer-duo" in report["typer_duo_error"]
        check = next(
            c for c in report["projects"][0]["checks"] if c["id"] == "subcommands-audit"
        )
        assert check["status"] == "skipped"

    def test_check_subcommands_off_skips_audit(self, tmp_path: Path):
        proj = tmp_path / "off"
        proj.mkdir()
        entry = _make_fake_cli(proj, cli_name="off", help_text=RICH_HELP_7)
        entry["commands_discovered"] = 7

        report = run_doctor({"off": entry})
        ids = {c["id"] for c in report["projects"][0]["checks"]}
        assert "subcommands-audit" not in ids
        assert "typer_duo_available" not in report
