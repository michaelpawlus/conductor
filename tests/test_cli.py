"""CLI-layer tests (focused on `conductor doctor`)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conductor.cli import app


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
    """Redirect CONDUCTOR_DIR / REGISTRY_PATH to a tmp location for the test."""
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


class TestDoctorCLI:
    def test_doctor_json_shape(self, tmp_path, isolated_conductor):
        proj = tmp_path / "okproj"
        proj.mkdir()
        entry = _make_fake_cli(proj, cli_name="okproj", help_text=RICH_HELP)
        entry["commands_discovered"] = 2
        _write_registry(isolated_conductor["registry"], {"okproj": entry})

        runner = CliRunner()
        result = runner.invoke(app, ["doctor", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "summary" in data
        assert data["summary"]["projects"] == 1
        assert data["projects"][0]["name"] == "okproj"
        assert data["projects"][0]["status"] == "ok"

    def test_doctor_exits_one_on_error(self, tmp_path, isolated_conductor):
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
        runner = CliRunner()
        result = runner.invoke(app, ["doctor", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.stdout)
        assert data["summary"]["errors"] == 1

    def test_doctor_exits_two_on_missing_registry(self, isolated_conductor):
        # Registry file intentionally not created.
        runner = CliRunner()
        result = runner.invoke(app, ["doctor", "--json"])
        assert result.exit_code == 2

    def test_doctor_check_subcommands_exits_one_when_typer_duo_missing(
        self, tmp_path, isolated_conductor, monkeypatch
    ):
        proj = tmp_path / "ok"
        proj.mkdir()
        entry = _make_fake_cli(proj, cli_name="ok", help_text=RICH_HELP)
        entry["commands_discovered"] = 2
        _write_registry(isolated_conductor["registry"], {"ok": entry})

        monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))

        runner = CliRunner()
        result = runner.invoke(app, ["doctor", "--check-subcommands", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.stdout)
        assert data["typer_duo_available"] is False
        assert "typer-duo" in data["typer_duo_error"]

    def test_doctor_fix_removes_stale_entries(self, tmp_path, isolated_conductor):
        proj = tmp_path / "alive"
        proj.mkdir()
        alive = _make_fake_cli(proj, cli_name="alive", help_text=RICH_HELP)
        alive["commands_discovered"] = 2
        _write_registry(
            isolated_conductor["registry"],
            {
                "alive": alive,
                "ghost": {
                    "path": str(tmp_path / "missing"),
                    "cli": "ghost",
                    "runtime": "python",
                    "commands_discovered": 0,
                },
            },
        )

        runner = CliRunner()
        result = runner.invoke(app, ["doctor", "--json", "--fix"])
        # After --fix removes the ghost entry, no errors remain.
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data.get("removed") == ["ghost"]

        # Registry file should no longer contain ghost.
        import yaml

        with open(isolated_conductor["registry"]) as f:
            saved = yaml.safe_load(f)
        assert "ghost" not in saved["projects"]
        assert "alive" in saved["projects"]
