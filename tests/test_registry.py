"""Tests for registry utilities."""

from pathlib import Path

from conductor.registry import (
    _parse_command_count,
    discover_projects,
    get_cli_command,
)


class TestGetCliCommand:
    def test_venv_entry(self, tmp_path: Path):
        # Create a fake venv binary
        venv_bin = tmp_path / ".venv" / "bin" / "my-cli"
        venv_bin.parent.mkdir(parents=True)
        venv_bin.touch()
        venv_bin.chmod(0o755)

        project = {
            "path": str(tmp_path),
            "cli": "my-cli",
            "runtime": "python",
            "venv": ".venv/bin/my-cli",
        }
        cmd, cwd = get_cli_command(project)
        assert cmd == [str(venv_bin)]
        assert cwd == tmp_path

    def test_python_with_venv(self, tmp_path: Path):
        # Create a fake venv python
        venv_python = tmp_path / ".venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        venv_python.touch()

        project = {
            "path": str(tmp_path),
            "cli": "python -m mymodule",
            "runtime": "python",
        }
        cmd, cwd = get_cli_command(project)
        assert cmd[0] == str(venv_python)
        assert cmd[1:] == ["-m", "mymodule"]
        assert cwd == tmp_path

    def test_npx_command(self, tmp_path: Path):
        project = {
            "path": str(tmp_path),
            "cli": "npx tsx cli/index.ts",
            "runtime": "node",
        }
        cmd, cwd = get_cli_command(project)
        assert cmd == ["npx", "tsx", "cli/index.ts"]
        assert cwd == tmp_path

    def test_bare_cli(self, tmp_path: Path):
        project = {
            "path": str(tmp_path),
            "cli": "my-tool",
            "runtime": "python",
        }
        cmd, cwd = get_cli_command(project)
        assert cmd == ["my-tool"]
        assert cwd == tmp_path


RICH_BOX_HELP = """
 Usage: conductor [OPTIONS] COMMAND [ARGS]...

 Compose project CLIs into declarative, chainable agent workflows.

╭─ Options ────────────────────────────────────────────────────────────────╮
│ --version                                                                │
│ --help                        Show this message and exit.                │
╰──────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────╮
│ discover   Auto-discover CLIs in ~/projects/ and update the registry.    │
│ list       List registered project CLIs and their commands.              │
│ run        Run a named or inline workflow.                               │
│ exec       Run a single project command with conductor wrapping.         │
│ workflows  List available workflow definitions.                          │
│ validate   Validate a workflow definition without running it.            │
│ history    Show recent workflow runs and outcomes.                       │
╰──────────────────────────────────────────────────────────────────────────╯
"""

PLAIN_CLICK_HELP = """Usage: foo [OPTIONS] COMMAND [ARGS]...

  A tool.

Options:
  --help  Show this message and exit.

Commands:
  foo  do foo
  bar  do bar
  baz  do baz
"""


class TestParseCommandCount:
    def test_rich_box_commands_counted(self):
        assert _parse_command_count(RICH_BOX_HELP) == 7

    def test_plain_click_commands_counted(self):
        assert _parse_command_count(PLAIN_CLICK_HELP) == 3

    def test_no_commands_section_returns_zero(self):
        assert _parse_command_count("Usage: foo [OPTIONS]\n") == 0

    def test_usage_line_does_not_trigger_commands_mode(self):
        text = "Usage: foo [OPTIONS]\n\nOptions:\n  --help  show help\n"
        assert _parse_command_count(text) == 0

    def test_options_only_not_counted_as_commands(self):
        # The Options box precedes Commands in Rich output; the parser must
        # skip it instead of counting its rows.
        assert _parse_command_count(RICH_BOX_HELP) == 7

    def test_empty_text_returns_zero(self):
        assert _parse_command_count("") == 0

    def test_rich_box_with_empty_commands_body_returns_zero(self):
        text = (
            "╭─ Commands ─╮\n"
            "╰────────────╯\n"
        )
        assert _parse_command_count(text) == 0

    def test_plain_click_two_commands(self):
        text = "Commands:\n  foo  do foo\n  bar  do bar\n"
        assert _parse_command_count(text) == 2

    def test_cap_exceeded_returns_zero(self):
        # Construct a synthetic Rich box with 300 command rows.
        rows = "\n".join(f"│ cmd{i}  desc │" for i in range(300))
        text = f"╭─ Commands ─╮\n{rows}\n╰────────────╯\n"
        assert _parse_command_count(text) == 0


class TestDiscoverProjectsIntegration:
    def test_discover_counts_commands_against_fixture_tree(
        self, tmp_path: Path, monkeypatch
    ):
        """Wire two on-disk fixture projects through `discover_projects` end to end.

        Each fixture ships a `pyproject.toml` (for `_discover_python`) and a
        `help.txt` (tracked). The venv wrapper that prints `help.txt` is
        synthesized here, because `.venv/` is git-ignored and we want the
        fixture tree to survive a fresh clone.
        """
        import conductor.registry as registry_mod
        import shutil

        fixtures_src = Path(__file__).parent / "fixtures" / "projects_for_count"
        assert fixtures_src.is_dir(), f"missing fixture dir: {fixtures_src}"

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        for child in fixtures_src.iterdir():
            if not child.is_dir():
                continue
            dst = projects_dir / child.name
            shutil.copytree(child, dst)

            help_file = dst / "help.txt"
            assert help_file.exists(), f"fixture {child.name} missing help.txt"
            cli_name = child.name  # matches [project.scripts] key
            bin_dir = dst / ".venv" / "bin"
            bin_dir.mkdir(parents=True)
            wrapper = bin_dir / cli_name
            wrapper.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "from pathlib import Path\n"
                f"HELP = Path(__file__).resolve().parents[2] / 'help.txt'\n"
                "if len(sys.argv) > 1 and sys.argv[1] == '--help':\n"
                "    sys.stdout.write(HELP.read_text())\n"
                "    sys.exit(0)\n"
                "sys.exit(2)\n"
            )
            wrapper.chmod(0o755)

        monkeypatch.setattr(registry_mod, "PROJECTS_DIR", projects_dir)

        discovered = discover_projects()

        assert "three-cmd" in discovered
        assert "five-cmd" in discovered
        assert discovered["three-cmd"]["commands_discovered"] == 3
        assert discovered["five-cmd"]["commands_discovered"] == 5
