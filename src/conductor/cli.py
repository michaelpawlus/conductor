"""Typer CLI — all conductor commands."""

import asyncio
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from . import __version__
from .doctor import (
    DOCTOR_SCHEMA_VERSION,
    diff_reports,
    load_baseline_report,
    run_doctor,
)
from .executor import execute_single, execute_workflow
from .history import get_history, get_run
from .output import (
    print_doctor,
    print_doctor_diff,
    print_error,
    print_history,
    print_json,
    print_registry,
    print_run_result,
    print_workflows,
)
from .registry import (
    REGISTRY_PATH,
    discover_projects,
    load_registry,
    save_registry,
)
from .workflow import (
    list_workflows,
    load_workflow,
    parse_workflow_yaml,
    validate_workflow,
)

app = typer.Typer(
    help="Compose project CLIs into declarative, chainable agent workflows.",
    no_args_is_help=True,
)
err = Console(stderr=True)

JsonFlag = Annotated[bool, typer.Option("--json", help="Output JSON to stdout")]


def _version_callback(value: bool) -> None:
    if value:
        print(f"conductor {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True),
    ] = False,
) -> None:
    """Agent Conductor — compose project CLIs into workflows."""


# --- discover ---


@app.command()
def discover(
    json_output: JsonFlag = False,
) -> None:
    """Auto-discover CLIs in ~/projects/ and update the registry."""
    err.print("Scanning ~/projects/ ...")
    projects = discover_projects()
    save_registry(projects)
    err.print(f"Found {len(projects)} project(s). Registry saved.")

    data = {
        name: {k: v for k, v in entry.items() if k != "name"}
        for name, entry in projects.items()
    }

    if json_output:
        print_json({"projects": data, "count": len(data)})
    else:
        print_registry(data, err)


# --- list ---


@app.command("list")
def list_projects(
    json_output: JsonFlag = False,
) -> None:
    """List registered project CLIs and their commands."""
    registry = load_registry()

    if json_output:
        print_json({"projects": registry, "count": len(registry)})
    else:
        print_registry(registry, err)


# --- run ---


@app.command()
def run(
    workflow: Annotated[
        str | None, typer.Argument(help="Workflow name or YAML file path")
    ] = None,
    inline: Annotated[
        str | None, typer.Option("--inline", help="Inline YAML workflow definition")
    ] = None,
    json_output: JsonFlag = False,
) -> None:
    """Run a named or inline workflow."""
    if not workflow and not inline:
        print_error("Provide a workflow name/path or --inline YAML", 1, json_output, err)
        raise typer.Exit(code=1)

    # Load workflow
    try:
        if inline:
            wf = parse_workflow_yaml(inline)
        else:
            wf = load_workflow(workflow)  # type: ignore[arg-type]
    except (FileNotFoundError, ValueError) as e:
        print_error(str(e), 1, json_output, err)
        raise typer.Exit(code=1)

    # Load registry and pre-validate
    registry = load_registry()
    issues = validate_workflow(wf, registry)
    errors = [i for i in issues if i["level"] == "error"]
    if errors:
        msgs = "; ".join(i["message"] for i in errors)
        print_error(msgs, 1, json_output, err)
        raise typer.Exit(code=1)

    if not json_output:
        deps = sum(1 for s in wf.steps if s.depends_on)
        err.print(
            f"Running [bold]{wf.name}[/bold] — "
            f"{len(wf.steps)} steps, {deps} with dependencies"
        )

    def _stderr_cb(step_id: str, text: str) -> None:
        if not json_output:
            for line in text.splitlines():
                err.print(f"  [{step_id}] {line}", style="dim")

    result = asyncio.run(execute_workflow(wf, registry, _stderr_cb))

    if json_output:
        print_json(result)
    else:
        print_run_result(result, err)


# --- exec ---


@app.command("exec")
def exec_cmd(
    project: Annotated[str, typer.Argument(help="Registered project name")],
    command: Annotated[str, typer.Argument(help="Command to run (quote if multi-word)")],
    json_output: JsonFlag = False,
) -> None:
    """Run a single project command with conductor wrapping."""
    registry = load_registry()
    if project not in registry:
        print_error(
            f"Project '{project}' not in registry. Run 'conductor discover' first.",
            2,
            json_output,
            err,
        )
        raise typer.Exit(code=2)

    result = asyncio.run(execute_single(project, command, registry))

    if json_output:
        print_json(result)
    else:
        status = result["status"]
        style = "green" if status == "completed" else "red"
        err.print(f"[{style}]{status}[/{style}] {project} {command}")
        if result.get("error"):
            err.print(f"  [red]{result['error']}[/red]")
        if result.get("output"):
            err.print_json(data=result["output"])


# --- workflows ---


@app.command()
def workflows(
    json_output: JsonFlag = False,
) -> None:
    """List available workflow definitions."""
    wfs = list_workflows()

    if json_output:
        print_json({"workflows": wfs, "count": len(wfs)})
    else:
        print_workflows(wfs, err)


# --- validate ---


@app.command()
def validate(
    workflow: Annotated[str, typer.Argument(help="Workflow name or YAML file path")],
    json_output: JsonFlag = False,
) -> None:
    """Validate a workflow definition without running it."""
    try:
        wf = load_workflow(workflow)
    except (FileNotFoundError, ValueError) as e:
        print_error(str(e), 1, json_output, err)
        raise typer.Exit(code=1)

    registry = load_registry()
    issues = validate_workflow(wf, registry)

    data = {
        "workflow": wf.name,
        "valid": len(issues) == 0,
        "steps": len(wf.steps),
        "issues": issues,
    }

    if json_output:
        print_json(data)
    else:
        if issues:
            err.print(f"[yellow]Workflow '{wf.name}' has {len(issues)} issue(s):[/yellow]")
            for issue in issues:
                err.print(f"  [{issue['level']}] step '{issue['step']}': {issue['message']}")
        else:
            err.print(f"[green]Workflow '{wf.name}' is valid ({len(wf.steps)} steps)[/green]")

    if issues:
        raise typer.Exit(code=1)


# --- doctor (sub-app: default snapshot + `diff` subcommand) ---


doctor_app = typer.Typer(
    no_args_is_help=False,
    help="Re-validate the registry and surface CLIs that need attention.",
)


def _require_registry(json_output: bool) -> None:
    if not REGISTRY_PATH.exists():
        print_error(
            f"Registry not found at {REGISTRY_PATH}. Run 'conductor discover' first.",
            2,
            json_output,
            err,
        )
        raise typer.Exit(code=2)


def _run_doctor_snapshot(
    *,
    project: str | None,
    check_json: bool,
    check_subcommands: bool,
) -> dict:
    registry = load_registry()
    return run_doctor(
        registry,
        project_filter=project,
        check_json=check_json,
        check_subcommands=check_subcommands,
    )


@doctor_app.callback(invoke_without_command=True)
def doctor(
    ctx: typer.Context,
    json_output: JsonFlag = False,
    project: Annotated[
        str | None,
        typer.Option("--project", help="Restrict checks to a single registered project"),
    ] = None,
    check_json: Annotated[
        bool,
        typer.Option("--check-json", help="Also probe whether CLI advertises --json"),
    ] = False,
    check_subcommands: Annotated[
        bool,
        typer.Option(
            "--check-subcommands",
            help="Run `typer-duo audit` on each project and report agent-readiness",
        ),
    ] = False,
    fix: Annotated[
        bool,
        typer.Option("--fix", help="Drop registry entries whose path no longer exists"),
    ] = False,
) -> None:
    """Re-validate the registry and surface CLIs that need attention."""
    # When a subcommand (e.g. `doctor diff`) is invoked, defer to it; the
    # subcommand carries its own option set.
    if ctx.invoked_subcommand is not None:
        return

    _require_registry(json_output)
    registry = load_registry()
    report = run_doctor(
        registry,
        project_filter=project,
        check_json=check_json,
        check_subcommands=check_subcommands,
    )

    removed: list[str] = []
    if fix:
        new_registry = dict(registry)
        for p in report["projects"]:
            for c in p["checks"]:
                if c["id"] == "path-exists" and c["status"] == "error":
                    if p["name"] in new_registry:
                        new_registry.pop(p["name"])
                        removed.append(p["name"])
                    break
        if removed:
            save_registry(new_registry)
        report["removed"] = removed

    if json_output:
        print_json(report)
    else:
        print_doctor(report, err)

    # After --fix, remaining errors are only those not resolved by removal.
    remaining_errors = sum(
        1
        for p in report["projects"]
        if p["status"] == "error" and p["name"] not in removed
    )
    if remaining_errors > 0 or report.get("typer_duo_available") is False:
        raise typer.Exit(code=1)


@doctor_app.command("diff")
def doctor_diff(
    since: Annotated[
        Path,
        typer.Option(
            "--since",
            help="Path to a prior doctor JSON report to diff the current run against",
        ),
    ],
    json_output: JsonFlag = False,
    project: Annotated[
        str | None,
        typer.Option("--project", help="Restrict checks to a single registered project"),
    ] = None,
    check_json: Annotated[
        bool,
        typer.Option("--check-json", help="Also probe whether CLI advertises --json"),
    ] = False,
    check_subcommands: Annotated[
        bool,
        typer.Option(
            "--check-subcommands",
            help="Run `typer-duo audit` on each project and report agent-readiness",
        ),
    ] = False,
) -> None:
    """Diff the current registry health against a prior doctor JSON snapshot."""
    try:
        baseline = load_baseline_report(since)
    except ValueError as exc:
        print_error(str(exc), 1, json_output, err)
        raise typer.Exit(code=1)

    _require_registry(json_output)
    current = _run_doctor_snapshot(
        project=project,
        check_json=check_json,
        check_subcommands=check_subcommands,
    )

    diff = diff_reports(baseline, current)

    envelope = {
        "schema_version": DOCTOR_SCHEMA_VERSION,
        "generated_at": current["checked_at"],
        **current,
        "diff": diff,
    }

    if json_output:
        print_json(envelope)
    else:
        print_doctor_diff(envelope, err)


app.add_typer(doctor_app, name="doctor")


# --- history ---


@app.command()
def history(
    limit: Annotated[int, typer.Option(help="Number of runs to show")] = 20,
    run_id: Annotated[
        int | None, typer.Option("--id", help="Show full result for a specific run")
    ] = None,
    json_output: JsonFlag = False,
) -> None:
    """Show recent workflow runs and outcomes."""
    if run_id is not None:
        result = get_run(run_id)
        if not result:
            print_error(f"Run {run_id} not found", 2, json_output, err)
            raise typer.Exit(code=2)
        if json_output:
            print_json(result)
        else:
            print_run_result(result, err)
        return

    runs = get_history(limit)

    if json_output:
        print_json({"runs": runs, "count": len(runs)})
    else:
        print_history(runs, err)
