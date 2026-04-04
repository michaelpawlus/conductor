"""Tests for registry utilities."""

from pathlib import Path

from conductor.registry import get_cli_command


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
