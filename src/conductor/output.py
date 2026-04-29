"""Result formatting — JSON to stdout, Rich to stderr."""

import json
import sys
from typing import Any

from rich.console import Console
from rich.table import Table


def print_json(data: Any) -> None:
    """Write JSON to stdout."""
    sys.stdout.write(json.dumps(data, indent=2) + "\n")


def print_error(message: str, code: int, as_json: bool, console: Console) -> None:
    """Print an error and exit with the given code."""
    if as_json:
        sys.stdout.write(json.dumps({"error": message, "code": code}) + "\n")
    else:
        console.print(f"[red]Error:[/red] {message}")


def print_run_result(result: dict[str, Any], console: Console) -> None:
    """Print a human-readable workflow run result to stderr."""
    status = result["status"]
    style = "green" if status == "completed" else "yellow" if status == "partial" else "red"

    console.print(f"\n[bold]Workflow:[/bold] {result['workflow']}")
    console.print(f"[bold]Status:[/bold]   [{style}]{status}[/{style}]")
    console.print(f"[bold]Duration:[/bold] {result['duration_seconds']}s\n")

    table = Table(show_header=True, header_style="bold", padding=(0, 1))
    table.add_column("Step", style="bold")
    table.add_column("Status")
    table.add_column("Duration", justify="right")
    table.add_column("Details")

    steps = result.get("steps", {})
    for step_id, step_data in steps.items():
        s = step_data["status"]
        if s == "completed":
            s_display = "[green]completed[/green]"
        elif s == "failed":
            s_display = "[red]failed[/red]"
        elif s in ("error", "timeout"):
            s_display = f"[red]{s}[/red]"
        elif s == "skipped":
            s_display = "[yellow]skipped[/yellow]"
        elif s == "parse_error":
            s_display = "[yellow]parse_error[/yellow]"
        else:
            s_display = s

        dur = f"{step_data.get('duration_ms', 0)}ms"
        details = step_data.get("error", "")
        if len(details) > 60:
            details = details[:57] + "..."

        table.add_row(step_id, s_display, dur, details)

    console.print(table)

    # Summary line
    total = len(steps)
    succeeded = sum(1 for s in steps.values() if s["status"] == "completed")
    failed = sum(1 for s in steps.values() if s["status"] in ("failed", "error", "timeout"))
    skipped = sum(1 for s in steps.values() if s["status"] == "skipped")

    parts = [f"{succeeded}/{total} succeeded"]
    if failed:
        parts.append(f"{failed} failed")
    if skipped:
        parts.append(f"{skipped} skipped")
    console.print(f"\n{', '.join(parts)}\n")


def print_registry(projects: dict[str, Any], console: Console) -> None:
    """Print the project registry as a human-readable table."""
    if not projects:
        console.print("[yellow]No projects registered. Run 'conductor discover' first.[/yellow]")
        return

    table = Table(show_header=True, header_style="bold", padding=(0, 1))
    table.add_column("Name", style="bold")
    table.add_column("Runtime")
    table.add_column("CLI")
    table.add_column("Commands", justify="right")
    table.add_column("Path")

    for name, entry in sorted(projects.items()):
        table.add_row(
            name,
            entry.get("runtime", "?"),
            entry.get("cli", "?"),
            str(entry.get("commands_discovered", 0)),
            entry.get("path", "?"),
        )

    console.print(table)


def print_workflows(workflows: list[dict[str, Any]], console: Console) -> None:
    """Print available workflows as a human-readable table."""
    if not workflows:
        console.print("[yellow]No workflows found.[/yellow]")
        return

    table = Table(show_header=True, header_style="bold", padding=(0, 1))
    table.add_column("Name", style="bold")
    table.add_column("Description")
    table.add_column("Steps", justify="right")
    table.add_column("Source")

    for wf in workflows:
        table.add_row(
            wf["name"],
            wf["description"],
            str(wf["steps"]),
            wf["source"],
        )

    console.print(table)


def _format_check_detail(check: dict[str, Any]) -> str:
    detail = check.get("detail", "")
    cid = check.get("id")
    if cid == "commands-discovered":
        return (
            f"live={check.get('live_count', '?')}, "
            f"registry={check.get('registry_count', '?')} ({detail})"
        )
    if cid == "subcommands-audit":
        score = check.get("score")
        findings = check.get("findings_count")
        if score is not None and findings is not None:
            return f"score={score}, findings={findings} ({check.get('severity_max', '?')})"
    return detail


def print_doctor(report: dict[str, Any], console: Console) -> None:
    """Print a doctor report as a compact grouped listing (errors, warnings, ok)."""
    summary = report.get("summary", {})
    total = summary.get("projects", 0)
    console.print(f"\n[bold]conductor doctor[/bold] — {total} projects checked\n")

    if report.get("typer_duo_available") is False:
        console.print(
            f"[red]Error:[/red] {report.get('typer_duo_error', 'typer-duo missing')}\n"
        )

    projects = report.get("projects", [])
    errors = [p for p in projects if p["status"] == "error"]
    warnings = [p for p in projects if p["status"] == "warning"]
    oks = [p for p in projects if p["status"] == "ok"]

    if errors:
        console.print(f"[red bold]ERROR[/red bold] ({len(errors)})")
        for p in errors:
            for c in p["checks"]:
                if c["status"] == "error":
                    console.print(
                        f"  {p['name']:<16} {c['id']}: {_format_check_detail(c)}"
                    )
        console.print("")

    if warnings:
        console.print(f"[yellow bold]WARNING[/yellow bold] ({len(warnings)})")
        for p in warnings:
            for c in p["checks"]:
                if c["status"] == "warning":
                    console.print(
                        f"  {p['name']:<16} {c['id']}: {_format_check_detail(c)}"
                    )
        console.print("")

    if oks:
        console.print(f"[green bold]OK[/green bold] ({len(oks)})")
        names = ", ".join(p["name"] for p in oks)
        console.print(f"  {names}")
        console.print("")

    audited = [
        (p, c)
        for p in projects
        for c in p["checks"]
        if c.get("id") == "subcommands-audit" and c.get("score") is not None
    ]
    if audited:
        console.print("[bold]Agent-readiness audit[/bold]")
        for p, c in audited:
            console.print(
                f"  {p['name']:<16} score={c['score']:<3} findings={c.get('findings_count', 0)}"
            )
        console.print("")

    removed = report.get("removed")
    if removed:
        console.print(f"[dim]Removed stale entries:[/dim] {', '.join(removed)}")


def print_history(runs: list[dict[str, Any]], console: Console) -> None:
    """Print run history as a human-readable table."""
    if not runs:
        console.print("[yellow]No workflow runs recorded yet.[/yellow]")
        return

    table = Table(show_header=True, header_style="bold", padding=(0, 1))
    table.add_column("ID", justify="right")
    table.add_column("Workflow", style="bold")
    table.add_column("Status")
    table.add_column("Duration", justify="right")
    table.add_column("Started At")

    for run in runs:
        status = run["status"]
        if status == "completed":
            s_display = "[green]completed[/green]"
        elif status == "partial":
            s_display = "[yellow]partial[/yellow]"
        else:
            s_display = f"[red]{status}[/red]"

        dur = f"{run.get('duration_seconds', 0)}s"
        table.add_row(
            str(run.get("id", "")),
            run["workflow"],
            s_display,
            dur,
            run.get("started_at", ""),
        )

    console.print(table)
