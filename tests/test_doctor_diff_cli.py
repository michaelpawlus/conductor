"""CLI-layer tests for `conductor doctor diff`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conductor.cli import app
from conductor.doctor import DOCTOR_SCHEMA_VERSION


def _write_registry(path: Path, data: dict) -> None:
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump({"projects": data}, f)


def _make_fake_cli(project_dir: Path, *, cli_name: str, help_text: str) -> dict:
    bin_dir = project_dir / ".venv" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / cli_name
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"HELP = {help_text!r}\n"
        "if len(sys.argv) > 1 and sys.argv[1] == '--help':\n"
        "    sys.stdout.write(HELP)\n"
        "    sys.exit(0)\n"
        "sys.exit(2)\n"
    )
    script.chmod(0o755)
    return {
        "path": str(project_dir),
        "cli": cli_name,
        "runtime": "python",
        "venv": f".venv/bin/{cli_name}",
        "commands_discovered": 0,
    }


RICH_HELP = (
    "\n Usage: foo [OPTIONS]\n\n"
    "╭─ Commands ─────────────╮\n"
    "│ a  do a                │\n"
    "│ b  do b                │\n"
    "╰────────────────────────╯\n"
)


@pytest.fixture
def isolated_conductor(tmp_path, monkeypatch):
    conductor_dir = tmp_path / ".conductor"
    registry_path = conductor_dir / "registry.yml"
    import conductor.registry as reg_mod
    import conductor.cli as cli_mod
    import conductor.doctor as doctor_mod

    monkeypatch.setattr(reg_mod, "CONDUCTOR_DIR", conductor_dir)
    monkeypatch.setattr(reg_mod, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(cli_mod, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(doctor_mod, "REGISTRY_PATH", registry_path)
    return {"dir": conductor_dir, "registry": registry_path}


class TestDoctorDiffCLI:
    def test_unreadable_since_exits_one(self, tmp_path, isolated_conductor):
        # Even with no registry, a missing --since file should error first
        # (the diff loader runs before registry validation).
        runner = CliRunner()
        result = runner.invoke(
            app,
            ["doctor", "diff", "--since", str(tmp_path / "nope.json"), "--json"],
        )
        assert result.exit_code == 1
        # JSON error payload on stdout
        data = json.loads(result.stdout)
        assert data["code"] == 1
        assert "cannot read" in data["error"]

    def test_malformed_since_exits_one_with_stderr(
        self, tmp_path, isolated_conductor
    ):
        bad = tmp_path / "bad.json"
        bad.write_text("{ not valid")
        runner = CliRunner()
        result = runner.invoke(
            app, ["doctor", "diff", "--since", str(bad)]
        )
        assert result.exit_code == 1
        assert "not valid JSON" in result.stderr

    def test_schema_mismatch_exits_one(self, tmp_path, isolated_conductor):
        f = tmp_path / "future.json"
        f.write_text(json.dumps({"schema_version": 999, "projects": []}))
        runner = CliRunner()
        result = runner.invoke(
            app, ["doctor", "diff", "--since", str(f), "--json"]
        )
        assert result.exit_code == 1
        data = json.loads(result.stdout)
        assert "schema_version" in data["error"]

    def test_valid_since_returns_zero_with_expected_envelope(
        self, tmp_path, isolated_conductor
    ):
        proj = tmp_path / "alpha"
        proj.mkdir()
        entry = _make_fake_cli(proj, cli_name="alpha", help_text=RICH_HELP)
        entry["commands_discovered"] = 2
        _write_registry(isolated_conductor["registry"], {"alpha": entry})

        baseline = tmp_path / "baseline.json"
        baseline.write_text(
            json.dumps(
                {
                    "checked_at": "2026-05-12T00:00:00Z",
                    "projects": [
                        {
                            "name": "alpha",
                            "path": str(proj),
                            "status": "ok",
                            "checks": [{"id": "path-exists", "status": "ok"}],
                        }
                    ],
                }
            )
        )

        runner = CliRunner()
        result = runner.invoke(
            app, ["doctor", "diff", "--since", str(baseline), "--json"]
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["schema_version"] == DOCTOR_SCHEMA_VERSION
        assert "generated_at" in data
        assert "projects" in data
        assert data["diff"]["vs_baseline"] == "2026-05-12T00:00:00Z"
        assert data["diff"]["regressed"] == []
        assert data["diff"]["improved"] == []
        assert data["diff"]["newly_added"] == []
        assert data["diff"]["removed"] == []

    def test_baseline_with_prior_diff_field_is_accepted(
        self, tmp_path, isolated_conductor
    ):
        proj = tmp_path / "alpha"
        proj.mkdir()
        entry = _make_fake_cli(proj, cli_name="alpha", help_text=RICH_HELP)
        entry["commands_discovered"] = 2
        _write_registry(isolated_conductor["registry"], {"alpha": entry})

        baseline = tmp_path / "baseline.json"
        baseline.write_text(
            json.dumps(
                {
                    "schema_version": DOCTOR_SCHEMA_VERSION,
                    "checked_at": "2026-05-12T00:00:00Z",
                    "generated_at": "2026-05-12T00:00:00Z",
                    "projects": [
                        {
                            "name": "alpha",
                            "path": str(proj),
                            "status": "ok",
                            "checks": [{"id": "path-exists", "status": "ok"}],
                        }
                    ],
                    "diff": {
                        "vs_baseline": "2026-05-11T00:00:00Z",
                        "regressed": [],
                        "improved": [],
                        "newly_added": [],
                        "removed": [],
                    },
                }
            )
        )

        runner = CliRunner()
        result = runner.invoke(
            app, ["doctor", "diff", "--since", str(baseline), "--json"]
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        # vs_baseline references the supplied file's snapshot, not its prior diff.
        assert data["diff"]["vs_baseline"] == "2026-05-12T00:00:00Z"

    def test_missing_registry_exits_two(self, tmp_path, isolated_conductor):
        # Valid --since but no registry on disk → exit 2.
        baseline = tmp_path / "baseline.json"
        baseline.write_text(
            json.dumps(
                {
                    "checked_at": "2026-05-12T00:00:00Z",
                    "projects": [],
                }
            )
        )
        runner = CliRunner()
        result = runner.invoke(
            app, ["doctor", "diff", "--since", str(baseline), "--json"]
        )
        assert result.exit_code == 2

    def test_regression_appears_in_diff(self, tmp_path, isolated_conductor):
        # Current snapshot will show an error (missing path), baseline had ok.
        _write_registry(
            isolated_conductor["registry"],
            {
                "ghost": {
                    "path": str(tmp_path / "missing"),
                    "cli": "ghost",
                    "runtime": "python",
                    "commands_discovered": 0,
                }
            },
        )

        baseline = tmp_path / "baseline.json"
        baseline.write_text(
            json.dumps(
                {
                    "checked_at": "2026-05-12T00:00:00Z",
                    "projects": [
                        {
                            "name": "ghost",
                            "path": str(tmp_path / "missing"),
                            "status": "ok",
                            "checks": [{"id": "path-exists", "status": "ok"}],
                        }
                    ],
                }
            )
        )

        runner = CliRunner()
        result = runner.invoke(
            app, ["doctor", "diff", "--since", str(baseline), "--json"]
        )
        # Diff still exits 0 even with regressions (per spec, no --fail-on-regression).
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert len(data["diff"]["regressed"]) == 1
        entry = data["diff"]["regressed"][0]
        assert entry["name"] == "ghost"
        assert entry["to"] == "error"
        assert entry["failed_check"] == "path-exists"
