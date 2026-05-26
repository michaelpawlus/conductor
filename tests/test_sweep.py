"""Tests for `conductor sweep` — the fan-out-across-the-registry primitive."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from conductor.cli import app
from conductor.sweep import (
    DEFAULT_PARALLEL,
    FILTER_MODES,
    _eligible_projects,
    _head_subcommand,
    run_sweep,
)


# --- fixture helpers ---


def _write_registry(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump({"projects": data}, f)


def _make_cli(
    project_dir: Path,
    *,
    cli_name: str,
    subcommand: str,
    exit_code: int = 0,
    output: str | None = None,
    error: str = "",
    delay_seconds: float = 0.0,
    advertise_subcommand: bool = True,
) -> dict:
    """Synthesize a venv binary that responds to --help and one subcommand.

    The fake CLI prints `output` to stdout and `error` to stderr when invoked
    with `subcommand`; with `--help` it prints a Rich Commands box that lists
    `subcommand` (or omits it if `advertise_subcommand=False`).
    """
    payload = output if output is not None else json.dumps({"project": cli_name})
    bin_dir = project_dir / ".venv" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / cli_name

    if advertise_subcommand:
        help_text = (
            "\n Usage: {name} [OPTIONS] COMMAND [ARGS]\n\n"
            "╭─ Commands ─────────────╮\n"
            "│ {sub}  do {sub}        │\n"
            "╰────────────────────────╯\n"
        ).format(name=cli_name, sub=subcommand)
    else:
        help_text = (
            "\n Usage: {name} [OPTIONS] COMMAND [ARGS]\n\n"
            "╭─ Commands ─────────────╮\n"
            "│ other  do other        │\n"
            "╰────────────────────────╯\n"
        ).format(name=cli_name)

    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time, json\n"
        f"HELP = {help_text!r}\n"
        f"SUB = {subcommand!r}\n"
        f"PAYLOAD = {payload!r}\n"
        f"ERR = {error!r}\n"
        f"DELAY = {delay_seconds!r}\n"
        f"EXIT = {exit_code!r}\n"
        "args = sys.argv[1:]\n"
        "if args and args[0] == '--help':\n"
        "    sys.stdout.write(HELP); sys.exit(0)\n"
        "if not args or args[0] != SUB:\n"
        "    sys.stderr.write(f'no such command\\n'); sys.exit(2)\n"
        "if DELAY: time.sleep(DELAY)\n"
        "if ERR: sys.stderr.write(ERR)\n"
        "sys.stdout.write(PAYLOAD)\n"
        "sys.exit(EXIT)\n"
    )
    script.chmod(0o755)
    return {
        "path": str(project_dir),
        "cli": cli_name,
        "runtime": "python",
        "venv": f".venv/bin/{cli_name}",
        "commands_discovered": 1,
    }


@pytest.fixture
def isolated_conductor(tmp_path, monkeypatch):
    """Redirect registry + history.db to a tmp location."""
    conductor_dir = tmp_path / ".conductor"
    registry_path = conductor_dir / "registry.yml"
    history_db = conductor_dir / "history.db"
    import conductor.registry as reg_mod
    import conductor.cli as cli_mod
    import conductor.doctor as doctor_mod
    import conductor.history as hist_mod

    monkeypatch.setattr(reg_mod, "CONDUCTOR_DIR", conductor_dir)
    monkeypatch.setattr(reg_mod, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(cli_mod, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(doctor_mod, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(hist_mod, "DB_PATH", history_db)
    return {"dir": conductor_dir, "registry": registry_path, "history": history_db}


# --- unit-level tests ---


class TestHeadSubcommand:
    def test_simple(self):
        assert _head_subcommand("doctor") == "doctor"

    def test_with_flags(self):
        assert _head_subcommand("doctor --json") == "doctor"

    def test_leading_flag_skipped(self):
        assert _head_subcommand("--verbose status") == "status"

    def test_only_flags_returns_none(self):
        assert _head_subcommand("--help") is None

    def test_empty(self):
        assert _head_subcommand("") is None


class TestFilterModes:
    def test_filter_modes_constant(self):
        assert "has-command" in FILTER_MODES
        assert "has-cli" in FILTER_MODES
        assert "all" in FILTER_MODES

    def test_default_parallel_positive(self):
        assert DEFAULT_PARALLEL >= 1


class TestEligibleProjectsFilter:
    def test_has_cli_takes_everyone(self, tmp_path):
        a = tmp_path / "a"
        a.mkdir()
        ea = _make_cli(a, cli_name="a", subcommand="doctor")
        b = tmp_path / "b"
        b.mkdir()
        eb = _make_cli(b, cli_name="b", subcommand="other", advertise_subcommand=False)
        registry = {"a": ea, "b": eb}

        eligible, skipped = _eligible_projects("doctor --json", registry, "has-cli")
        assert eligible == ["a", "b"]
        assert skipped == {}

    def test_all_alias_takes_everyone(self, tmp_path):
        a = tmp_path / "a"
        a.mkdir()
        ea = _make_cli(a, cli_name="a", subcommand="doctor")
        eligible, skipped = _eligible_projects("doctor", {"a": ea}, "all")
        assert eligible == ["a"]
        assert skipped == {}

    def test_has_command_filters_by_help(self, tmp_path):
        a = tmp_path / "a"
        a.mkdir()
        ea = _make_cli(a, cli_name="a", subcommand="doctor")
        b = tmp_path / "b"
        b.mkdir()
        # b's help advertises "other", not "doctor"
        eb = _make_cli(b, cli_name="b", subcommand="other", advertise_subcommand=False)
        registry = {"a": ea, "b": eb}

        eligible, skipped = _eligible_projects("doctor --json", registry, "has-command")
        assert eligible == ["a"]
        assert "b" in skipped
        assert "doctor" in skipped["b"]

    def test_unknown_filter_raises(self):
        with pytest.raises(ValueError):
            _eligible_projects("doctor", {}, "bogus")


# --- run_sweep integration tests ---


class TestRunSweep:
    def test_completed_envelope_shape(self, tmp_path, isolated_conductor):
        proj = tmp_path / "alpha"
        proj.mkdir()
        entry = _make_cli(
            proj,
            cli_name="alpha",
            subcommand="doctor",
            output=json.dumps({"ok": True}),
        )
        registry = {"alpha": entry}

        result = asyncio.run(run_sweep("doctor", registry, filter_mode="has-cli"))

        assert result["status"] == "completed"
        assert result["command"] == "doctor"
        assert result["filter"] == "has-cli"
        assert result["kind"] == "sweep"
        assert result["workflow"].startswith("_sweep:")
        assert result["summary"] == {
            "total": 1,
            "completed": 1,
            "failed": 0,
            "skipped": 0,
        }
        row = result["results"][0]
        assert row["project"] == "alpha"
        assert row["status"] == "completed"
        assert row["exit_code"] == 0
        assert row["output"] == {"ok": True}

    def test_partial_failure_contained_with_continue(self, tmp_path, isolated_conductor):
        ok = tmp_path / "ok"
        ok.mkdir()
        ok_entry = _make_cli(ok, cli_name="ok", subcommand="doctor")
        bad = tmp_path / "bad"
        bad.mkdir()
        bad_entry = _make_cli(
            bad,
            cli_name="bad",
            subcommand="doctor",
            exit_code=1,
            error="boom",
        )
        registry = {"ok": ok_entry, "bad": bad_entry}

        result = asyncio.run(
            run_sweep("doctor", registry, filter_mode="has-cli", continue_on_error=True)
        )

        statuses = {r["project"]: r["status"] for r in result["results"]}
        assert statuses == {"ok": "completed", "bad": "failed"}
        assert result["status"] == "partial"
        assert result["summary"]["failed"] == 1
        assert result["summary"]["completed"] == 1

    def test_has_command_filter_skips_unsupported(self, tmp_path, isolated_conductor):
        ok = tmp_path / "ok"
        ok.mkdir()
        ok_entry = _make_cli(ok, cli_name="ok", subcommand="doctor")
        other = tmp_path / "other"
        other.mkdir()
        other_entry = _make_cli(
            other,
            cli_name="other",
            subcommand="status",
            advertise_subcommand=False,
        )
        registry = {"ok": ok_entry, "other": other_entry}

        result = asyncio.run(
            run_sweep("doctor", registry, filter_mode="has-command")
        )

        statuses = {r["project"]: r["status"] for r in result["results"]}
        assert statuses["ok"] == "completed"
        assert statuses["other"] == "skipped"
        assert result["summary"]["skipped"] == 1

    def test_parallel_cap_respected(self, tmp_path, isolated_conductor):
        """With parallel=1 and three 250ms tasks, total wall time >= 0.75s."""
        registry: dict[str, dict] = {}
        for n in ("a", "b", "c"):
            proj = tmp_path / n
            proj.mkdir()
            registry[n] = _make_cli(
                proj, cli_name=n, subcommand="doctor", delay_seconds=0.25
            )

        import time

        t0 = time.monotonic()
        result = asyncio.run(
            run_sweep("doctor", registry, filter_mode="has-cli", parallel=1)
        )
        elapsed = time.monotonic() - t0

        assert result["status"] == "completed"
        assert elapsed >= 0.6, f"serial dispatch should take >= 0.6s, took {elapsed:.2f}s"

    def test_history_persistence(self, tmp_path, isolated_conductor):
        proj = tmp_path / "alpha"
        proj.mkdir()
        entry = _make_cli(proj, cli_name="alpha", subcommand="doctor")
        asyncio.run(run_sweep("doctor", {"alpha": entry}, filter_mode="has-cli"))

        # Re-import to pick up the patched DB_PATH for the inspection call.
        from conductor.history import get_history

        runs = get_history(limit=5)
        assert len(runs) == 1
        assert runs[0]["workflow"] == "_sweep:doctor"
        assert runs[0]["status"] == "completed"


# --- CLI-layer integration ---


class TestSweepCLI:
    def test_sweep_json_envelope(self, tmp_path, isolated_conductor):
        proj = tmp_path / "alpha"
        proj.mkdir()
        entry = _make_cli(
            proj,
            cli_name="alpha",
            subcommand="doctor",
            output=json.dumps({"ok": True}),
        )
        _write_registry(isolated_conductor["registry"], {"alpha": entry})

        runner = CliRunner()
        result = runner.invoke(
            app, ["sweep", "--filter", "has-cli", "--json", "doctor"]
        )
        assert result.exit_code == 0, result.stdout + (result.stderr or "")
        data = json.loads(result.stdout)
        assert data["summary"]["completed"] == 1
        assert data["results"][0]["project"] == "alpha"
        assert data["results"][0]["output"] == {"ok": True}

    def test_sweep_exits_two_on_empty_registry(self, isolated_conductor):
        # No registry written.
        runner = CliRunner()
        result = runner.invoke(app, ["sweep", "--json", "doctor"])
        assert result.exit_code == 2

    def test_sweep_continue_on_error_default_exits_zero(
        self, tmp_path, isolated_conductor
    ):
        ok = tmp_path / "ok"
        ok.mkdir()
        ok_entry = _make_cli(ok, cli_name="ok", subcommand="doctor")
        bad = tmp_path / "bad"
        bad.mkdir()
        bad_entry = _make_cli(
            bad, cli_name="bad", subcommand="doctor", exit_code=1, error="x"
        )
        _write_registry(
            isolated_conductor["registry"], {"ok": ok_entry, "bad": bad_entry}
        )

        runner = CliRunner()
        result = runner.invoke(
            app, ["sweep", "--filter", "has-cli", "--json", "doctor"]
        )
        # default continue-on-error means partial failure is exit 0
        assert result.exit_code == 0

    def test_sweep_fail_fast_exits_one_on_failure(
        self, tmp_path, isolated_conductor
    ):
        bad = tmp_path / "bad"
        bad.mkdir()
        bad_entry = _make_cli(
            bad, cli_name="bad", subcommand="doctor", exit_code=1, error="x"
        )
        _write_registry(isolated_conductor["registry"], {"bad": bad_entry})

        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "sweep",
                "--filter",
                "has-cli",
                "--fail-fast",
                "--json",
                "doctor",
            ],
        )
        assert result.exit_code == 1
