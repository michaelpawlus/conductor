"""Project discovery and registry management."""

import json
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import yaml

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


def _count_commands(project_dir: Path, cli_cmd: str) -> int:
    """Count CLI commands by parsing --help output."""
    try:
        result = subprocess.run(
            cli_cmd.split() + ["--help"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(project_dir),
        )
        if result.returncode != 0:
            return 0

        # Count lines in the Commands section (Typer/Click convention)
        lines = result.stdout.split("\n")
        in_commands = False
        count = 0
        for line in lines:
            stripped = line.strip().lower()
            if stripped.startswith("commands") or stripped.startswith("usage"):
                in_commands = True
                continue
            if in_commands and line and not line[0].isspace():
                in_commands = False
            if in_commands and line.strip():
                # Lines with a command name followed by description
                parts = line.strip().split()
                if parts and parts[0].isidentifier():
                    count += 1
        return max(count, 1)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return 0


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
