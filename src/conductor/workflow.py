"""Workflow parsing, validation, and Pydantic models."""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, field_validator

CONDUCTOR_DIR = Path.home() / ".conductor"
USER_WORKFLOWS_DIR = CONDUCTOR_DIR / "workflows"
BUILTIN_WORKFLOWS_DIR = Path(__file__).parent / "workflows"


class Step(BaseModel):
    """A single step in a workflow."""

    id: str
    project: str
    command: str
    depends_on: list[str] = []
    input_from: str | None = None
    timeout: int = 120
    on_fail: str = "stop"
    condition: str | None = None
    extract: str | None = None

    @field_validator("on_fail")
    @classmethod
    def validate_on_fail(cls, v: str) -> str:
        if v in ("stop", "skip"):
            return v
        if v.startswith("retry:"):
            try:
                n = int(v.split(":")[1])
                if n > 0:
                    return v
            except (ValueError, IndexError):
                pass
        raise ValueError(f"Invalid on_fail: {v!r}. Must be 'stop', 'skip', or 'retry:N'")


class Workflow(BaseModel):
    """A declarative workflow definition."""

    name: str
    description: str = ""
    parallel_default: bool = True
    steps: list[Step]

    @field_validator("steps")
    @classmethod
    def validate_steps(cls, steps: list[Step]) -> list[Step]:
        ids = [s.id for s in steps]

        # Unique IDs
        if len(ids) != len(set(ids)):
            dupes = {i for i in ids if ids.count(i) > 1}
            raise ValueError(f"Duplicate step IDs: {dupes}")

        id_set = set(ids)

        # input_from implies depends_on
        for step in steps:
            if step.input_from:
                if step.input_from not in id_set:
                    raise ValueError(
                        f"Step '{step.id}' input_from references unknown step "
                        f"'{step.input_from}'"
                    )
                if step.input_from not in step.depends_on:
                    step.depends_on.append(step.input_from)

        # Validate depends_on references
        for step in steps:
            for dep in step.depends_on:
                if dep not in id_set:
                    raise ValueError(
                        f"Step '{step.id}' depends on unknown step '{dep}'"
                    )

        # Cycle detection
        _detect_cycles(steps)

        return steps


def _detect_cycles(steps: list[Step]) -> None:
    """Detect cycles in the dependency graph using DFS."""
    graph: dict[str, list[str]] = {s.id: list(s.depends_on) for s in steps}
    visited: set[str] = set()
    rec_stack: set[str] = set()

    def dfs(node: str) -> bool:
        visited.add(node)
        rec_stack.add(node)
        for neighbor in graph.get(node, []):
            if neighbor not in visited:
                if dfs(neighbor):
                    return True
            elif neighbor in rec_stack:
                return True
        rec_stack.discard(node)
        return False

    for step in steps:
        if step.id not in visited:
            if dfs(step.id):
                raise ValueError("Cycle detected in step dependencies")


def load_workflow(name_or_path: str) -> Workflow:
    """Load a workflow from a name or file path.

    Resolution order:
    1. Direct file path (absolute or relative)
    2. ~/.conductor/workflows/<name>.yml
    3. Built-in workflows bundled with conductor
    """
    path = Path(name_or_path)

    if path.exists() and path.is_file():
        return _parse_workflow_file(path)

    # Try user workflows directory
    user_path = USER_WORKFLOWS_DIR / f"{name_or_path}.yml"
    if user_path.exists():
        return _parse_workflow_file(user_path)

    # Try built-in workflows
    builtin_path = BUILTIN_WORKFLOWS_DIR / f"{name_or_path}.yml"
    if builtin_path.exists():
        return _parse_workflow_file(builtin_path)

    raise FileNotFoundError(
        f"Workflow not found: {name_or_path!r}. "
        f"Searched: {path}, {user_path}, {builtin_path}"
    )


def parse_workflow_yaml(yaml_str: str) -> Workflow:
    """Parse a workflow from a YAML string."""
    data = yaml.safe_load(yaml_str)
    if not isinstance(data, dict):
        raise ValueError("Workflow YAML must be a mapping")
    return Workflow(**data)


def _parse_workflow_file(path: Path) -> Workflow:
    """Parse a workflow from a YAML file."""
    with open(path) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Workflow file must contain a YAML mapping: {path}")
    return Workflow(**data)


def list_workflows() -> list[dict[str, Any]]:
    """List all available workflows from user and built-in directories."""
    workflows: list[dict[str, Any]] = []
    seen: set[str] = set()

    for wf_dir, source in [
        (USER_WORKFLOWS_DIR, "user"),
        (BUILTIN_WORKFLOWS_DIR, "builtin"),
    ]:
        if not wf_dir.is_dir():
            continue
        for yml_path in sorted(wf_dir.glob("*.yml")):
            name = yml_path.stem
            if name in seen:
                continue
            seen.add(name)
            try:
                wf = _parse_workflow_file(yml_path)
                workflows.append(
                    {
                        "name": wf.name,
                        "description": wf.description,
                        "steps": len(wf.steps),
                        "source": source,
                        "path": str(yml_path),
                    }
                )
            except Exception as e:
                workflows.append(
                    {
                        "name": name,
                        "description": f"(parse error: {e})",
                        "steps": 0,
                        "source": source,
                        "path": str(yml_path),
                    }
                )

    return workflows


def validate_workflow(
    workflow: Workflow, registry: dict[str, Any]
) -> list[dict[str, str]]:
    """Validate a workflow against the registry. Returns a list of issues."""
    issues: list[dict[str, str]] = []

    for step in workflow.steps:
        if step.project not in registry:
            issues.append(
                {
                    "step": step.id,
                    "level": "error",
                    "message": (
                        f"Project '{step.project}' not found in registry. "
                        f"Run 'conductor discover' or add it manually to "
                        f"~/.conductor/registry.yml"
                    ),
                }
            )

    return issues
