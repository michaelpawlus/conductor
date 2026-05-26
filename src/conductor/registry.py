"""Project discovery and registry management."""

import json
import os
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import yaml

_MAX_COMMANDS = 200
_HELP_ENV = {"NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "200"}
_RICH_COMMANDS_HEADER = re.compile(r"^[╭─\s]*Commands?\s*[─╮\s]*$")
_PLAIN_COMMANDS_HEADER = re.compile(r"^\s*Commands?:\s*$")
_RICH_BOX_END = re.compile(r"^[╰─\s]*[╯╰].*$")

PROJECTS_DIR = Path.home() / "projects"
CONDUCTOR_DIR = Path.home() / ".conductor"
REGISTRY_PATH = CONDUCTOR_DIR / "registry.yml"


def discover_projects() -> dict[str, dict[str, Any]]:
    """Scan ~/projects/ for CLI-enabled projects."""
    projects: dict[str, dict[str, Any]] = {}

    if not PROJECTS_DIR.is_dir():
        return projects

    for project_dir in sorted(PROJECTS_DIR.iterdir()):
        if not project_dir.is_dir() or project_dir.name.startswith("."):
            continue

        # Try Python projects first
        pyproject = project_dir / "pyproject.toml"
        if pyproject.exists():
            entry = _discover_python(project_dir, pyproject)
            if entry:
                projects[entry["name"]] = entry
                continue

        # Try Node projects
        package_json = project_dir / "package.json"
        if package_json.exists():
            entry = _discover_node(project_dir, package_json)
            if entry:
                projects[entry["name"]] = entry
                continue

    return projects


def _discover_python(project_dir: Path, pyproject: Path) -> dict[str, Any] | None:
    """Discover a Python project's CLI from pyproject.toml."""
    with open(pyproject, "rb") as f:
        data = tomllib.load(f)

    scripts = data.get("project", {}).get("scripts", {})
    if not scripts:
        # Check for __main__.py pattern
        return _discover_python_module(project_dir, data)

    cli_name = next(iter(scripts))

    # Check for venv binary
    venv_bin = project_dir / ".venv" / "bin" / cli_name
    venv = f".venv/bin/{cli_name}" if venv_bin.exists() else None

    commands_count = _count_commands(project_dir, str(venv_bin) if venv else cli_name)

    entry: dict[str, Any] = {
        "name": cli_name,
        "path": str(project_dir),
        "cli": cli_name,
        "runtime": "python",
        "commands_discovered": commands_count,
    }
    if venv:
        entry["venv"] = venv

    return entry


def _discover_python_module(
    project_dir: Path, pyproject_data: dict
) -> dict[str, Any] | None:
    """Discover a Python project that uses python -m <module> as its CLI."""
    src_dir = project_dir / "src"
    candidates = []

    # Look for packages with __main__.py
    search_dirs = [project_dir, src_dir] if src_dir.exists() else [project_dir]
    for search in search_dirs:
        if not search.is_dir():
            continue
        for child in search.iterdir():
            if child.is_dir() and (child / "__main__.py").exists():
                candidates.append(child.name)

    if not candidates:
        return None

    module_name = candidates[0]
    cli_cmd = f"python -m {module_name}"

    return {
        "name": module_name,
        "path": str(project_dir),
        "cli": cli_cmd,
        "runtime": "python",
        "commands_discovered": 0,
    }


def _discover_node(project_dir: Path, package_json: Path) -> dict[str, Any] | None:
    """Discover a Node.js project's CLI."""
    with open(package_json) as f:
        data = json.load(f)

    name = data.get("name", project_dir.name)
    bin_field = data.get("bin")

    cli_entry = None
    if isinstance(bin_field, dict):
        cli_name = next(iter(bin_field))
        cli_entry = bin_field[cli_name]
        name = cli_name
    elif isinstance(bin_field, str):
        cli_entry = bin_field

    # Check for cli/ directory with TypeScript entry
    if not cli_entry:
        cli_index = project_dir / "cli" / "index.ts"
        if cli_index.exists():
            cli_entry = "cli/index.ts"

    if not cli_entry:
        return None

    if cli_entry.endswith(".ts"):
        cli = f"npx tsx {cli_entry}"
    else:
        cli = name

    return {
        "name": name,
        "path": str(project_dir),
        "cli": cli,
        "runtime": "node",
        "commands_discovered": 0,
    }


def _capture_help(project_dir: Path, cli_cmd: str) -> str | None:
    """Run `<cli> --help` and return stdout, or None on failure."""
    try:
        result = subprocess.run(
            cli_cmd.split() + ["--help"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(project_dir),
            env={**os.environ, **_HELP_ENV},
        )
    except (
        subprocess.TimeoutExpired,
        subprocess.SubprocessError,
        FileNotFoundError,
        OSError,
    ):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _parse_command_count(help_text: str) -> int:
    """Parse a Typer/Click `--help` capture and return the subcommand count.

    Recognizes both the Rich box format ("╭─ Commands ─╮ / │ name ... │ / ╰─╯")
    and the plain Click format ("Commands:\\n  name  description\\n"). Returns
    0 when no Commands section is found; caps at _MAX_COMMANDS to avoid poisoning
    the registry with runaway parser output.
    """
    return len(_parse_command_names(help_text))


def _parse_command_names(help_text: str) -> list[str]:
    """Parse a Typer/Click `--help` capture and return subcommand names in order.

    Companion to `_parse_command_count`; same parsing rules and cap behavior.
    Returns ``[]`` when no Commands section is found or when the parser would
    exceed `_MAX_COMMANDS` (runaway output protection).
    """
    lines = help_text.split("\n")
    names = _names_rich_box(lines)
    if not names:
        names = _names_plain_click(lines)
    if len(names) > _MAX_COMMANDS:
        return []
    return names


def _names_rich_box(lines: list[str]) -> list[str]:
    names: list[str] = []
    i = 0
    while i < len(lines):
        if _RICH_COMMANDS_HEADER.match(lines[i]):
            i += 1
            while i < len(lines):
                line = lines[i]
                stripped = line.strip()
                if not stripped:
                    i += 1
                    continue
                if stripped[0] in "╰╯" or _RICH_BOX_END.match(line):
                    break
                if stripped.startswith("│"):
                    inner = stripped.lstrip("│").strip()
                    if inner:
                        name = inner.split()[0]
                        if name.replace("-", "_").replace(":", "_").isidentifier() or _looks_like_command(name):
                            names.append(name)
                i += 1
            return names
        i += 1
    return names


def _names_plain_click(lines: list[str]) -> list[str]:
    names: list[str] = []
    in_commands = False
    for line in lines:
        if _PLAIN_COMMANDS_HEADER.match(line):
            in_commands = True
            continue
        if in_commands:
            if not line.strip():
                break
            if not line[0].isspace():
                break
            parts = line.strip().split()
            if parts:
                name = parts[0]
                if _looks_like_command(name):
                    names.append(name)
    return names


def _looks_like_command(token: str) -> bool:
    """Return True if `token` resembles a CLI command name (letters, digits, - _ :)."""
    if not token:
        return False
    return all(c.isalnum() or c in "-_:" for c in token) and not token[0].isdigit()


def _count_commands(project_dir: Path, cli_cmd: str) -> int:
    """Count CLI commands by parsing --help output."""
    help_text = _capture_help(project_dir, cli_cmd)
    if help_text is None:
        return 0
    return _parse_command_count(help_text)


def list_project_commands(project: dict[str, Any]) -> list[str]:
    """Return the subcommand names exposed by a registered project's CLI.

    Captures the CLI's top-level ``--help`` and parses the Commands section.
    Returns ``[]`` when the CLI cannot be resolved, when ``--help`` fails, or
    when the parser cannot find a Commands section. Used by ``conductor sweep``
    to decide whether a project exposes the head subcommand being fanned out.
    """
    try:
        cmd_parts, cwd = get_cli_command(project)
    except Exception:
        return []
    if not cmd_parts:
        return []
    try:
        result = subprocess.run(
            cmd_parts + ["--help"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(cwd),
            env={**os.environ, **_HELP_ENV},
        )
    except (
        subprocess.TimeoutExpired,
        subprocess.SubprocessError,
        FileNotFoundError,
        OSError,
    ):
        return []
    if result.returncode != 0:
        return []
    return _parse_command_names(result.stdout)


def load_registry() -> dict[str, dict[str, Any]]:
    """Load the project registry from disk."""
    if not REGISTRY_PATH.exists():
        return {}
    with open(REGISTRY_PATH) as f:
        data = yaml.safe_load(f) or {}
    return data.get("projects", {})


def save_registry(projects: dict[str, dict[str, Any]]) -> None:
    """Save the project registry to disk."""
    CONDUCTOR_DIR.mkdir(parents=True, exist_ok=True)

    # Strip the 'name' key from entries (it's the dict key already)
    cleaned = {}
    for name, entry in projects.items():
        cleaned[name] = {k: v for k, v in entry.items() if k != "name"}

    with open(REGISTRY_PATH, "w") as f:
        yaml.dump(
            {"projects": cleaned}, f, default_flow_style=False, sort_keys=False
        )


def get_cli_command(project: dict[str, Any]) -> tuple[list[str], Path]:
    """Get the executable command parts and working directory for a project.

    Returns (command_parts, cwd).
    """
    path = Path(project["path"])
    cli = project["cli"]

    # If venv entry points to a specific binary, use it directly
    if "venv" in project:
        venv_path = path / project["venv"]
        if venv_path.exists():
            return ([str(venv_path)], path)

    parts = cli.split()

    # If cli starts with python and there's a .venv, use venv's python
    if parts[0] == "python":
        venv_python = path / ".venv" / "bin" / "python"
        if venv_python.exists():
            parts[0] = str(venv_python)

    return (parts, path)
