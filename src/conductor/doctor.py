"""Registry health checks — read-only diagnostics for `conductor list`."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .registry import (
    REGISTRY_PATH,
    _HELP_ENV,
    _parse_command_count,
    get_cli_command,
)

_HELP_TIMEOUT = 10
_AUDIT_TIMEOUT = 30


def run_doctor(
    registry: dict[str, dict[str, Any]],
    *,
    project_filter: str | None = None,
    check_json: bool = False,
    check_subcommands: bool = False,
) -> dict[str, Any]:
    """Run per-project health checks and return a structured report.

    Pure function; never mutates the registry. CLI plumbing (including
    registry loading, `--fix`, exit codes) stays in cli.py.
    """
    reports: list[dict[str, Any]] = []
    summary = {"projects": 0, "ok": 0, "warnings": 0, "errors": 0}

    items = registry.items()
    if project_filter is not None:
        items = [(name, entry) for name, entry in items if name == project_filter]

    typer_duo_path: str | None = None
    typer_duo_missing = False
    if check_subcommands:
        typer_duo_path = shutil.which("typer-duo")
        typer_duo_missing = typer_duo_path is None

    for name, entry in sorted(items):
        project_report = _check_project(
            name,
            entry,
            check_json=check_json,
            check_subcommands=check_subcommands,
            typer_duo_path=typer_duo_path,
        )
        reports.append(project_report)
        status = project_report["status"]
        summary["projects"] += 1
        if status == "error":
            summary["errors"] += 1
        elif status == "warning":
            summary["warnings"] += 1
        else:
            summary["ok"] += 1

    report: dict[str, Any] = {
        "checked_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "registry_path": str(REGISTRY_PATH),
        "summary": summary,
        "projects": reports,
    }
    if check_subcommands:
        report["typer_duo_available"] = not typer_duo_missing
        if typer_duo_missing:
            report["typer_duo_error"] = (
                "typer-duo not found on PATH; install typer-duo to enable --check-subcommands"
            )
    return report


def _check_project(
    name: str,
    entry: dict[str, Any],
    *,
    check_json: bool,
    check_subcommands: bool = False,
    typer_duo_path: str | None = None,
) -> dict[str, Any]:
    """Run all checks for a single registry entry."""
    checks: list[dict[str, Any]] = []
    project = {"name": name, **entry}

    path_check = _check_path_exists(project)
    checks.append(path_check)

    if path_check["status"] != "ok":
        checks.append({"id": "cli-resolves", "status": "skipped", "detail": "path-exists failed"})
        checks.append({"id": "help-parses", "status": "skipped", "detail": "path-exists failed"})
        if check_subcommands:
            checks.append(
                {
                    "id": "subcommands-audit",
                    "status": "skipped",
                    "detail": "path-exists failed",
                }
            )
    else:
        cli_check, cmd_parts, cwd = _check_cli_resolves(project)
        checks.append(cli_check)

        if cli_check["status"] != "ok" or cmd_parts is None:
            checks.append(
                {"id": "help-parses", "status": "skipped", "detail": "cli-resolves failed"}
            )
            help_text: str | None = None
        else:
            help_check, help_text = _check_help_parses(cmd_parts, cwd)
            checks.append(help_check)

        if entry.get("runtime") != "node":
            checks.append(_check_commands_discovered(entry, help_text))

        if check_json:
            checks.append(_check_json_flag(help_text))

        if check_subcommands:
            checks.append(_check_subcommands_audit(entry, typer_duo_path))

    venv_check = _check_venv_present(entry)
    if venv_check is not None:
        checks.append(venv_check)

    status = _rollup(checks)
    return {
        "name": name,
        "path": entry.get("path", ""),
        "status": status,
        "checks": checks,
    }


def _check_path_exists(project: dict[str, Any]) -> dict[str, Any]:
    path = project.get("path")
    if path and Path(path).is_dir():
        return {"id": "path-exists", "status": "ok"}
    return {
        "id": "path-exists",
        "status": "error",
        "detail": f"path does not exist: {path}",
    }


def _check_cli_resolves(
    project: dict[str, Any],
) -> tuple[dict[str, Any], list[str] | None, Path | None]:
    try:
        cmd_parts, cwd = get_cli_command(project)
    except Exception as exc:  # noqa: BLE001
        return (
            {"id": "cli-resolves", "status": "error", "detail": str(exc)},
            None,
            None,
        )

    if not cmd_parts:
        return (
            {
                "id": "cli-resolves",
                "status": "error",
                "detail": "no [project.scripts] entry and no __main__.py",
            },
            None,
            None,
        )

    first = cmd_parts[0]
    # If it's an absolute path or contains a slash, the binary must exist.
    if ("/" in first or first.startswith(".")) and not Path(first).exists():
        return (
            {
                "id": "cli-resolves",
                "status": "error",
                "detail": f"binary not found: {first}",
            },
            None,
            None,
        )
    return ({"id": "cli-resolves", "status": "ok"}, cmd_parts, cwd)


def _check_help_parses(
    cmd_parts: list[str], cwd: Path
) -> tuple[dict[str, Any], str | None]:
    try:
        result = subprocess.run(
            cmd_parts + ["--help"],
            capture_output=True,
            text=True,
            timeout=_HELP_TIMEOUT,
            cwd=str(cwd),
            env={**os.environ, **_HELP_ENV},
        )
    except subprocess.TimeoutExpired:
        return (
            {
                "id": "help-parses",
                "status": "warning",
                "detail": f"--help did not return within {_HELP_TIMEOUT}s",
            },
            None,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        return (
            {"id": "help-parses", "status": "error", "detail": str(exc)},
            None,
        )

    if result.returncode != 0:
        return (
            {
                "id": "help-parses",
                "status": "error",
                "detail": f"--help exited with code {result.returncode}",
            },
            None,
        )
    return ({"id": "help-parses", "status": "ok"}, result.stdout)


def _check_commands_discovered(
    entry: dict[str, Any], help_text: str | None
) -> dict[str, Any]:
    registry_count = int(entry.get("commands_discovered", 0) or 0)
    if help_text is None:
        return {
            "id": "commands-discovered",
            "status": "skipped",
            "detail": "help-parses failed or skipped",
            "registry_count": registry_count,
        }

    live_count = _parse_command_count(help_text)

    if live_count != registry_count:
        # Drift: either the registry was populated before the parser fix, or
        # the CLI's command surface changed since the last `discover`.
        if live_count == 0 and registry_count > 0:
            detail = "parser found nothing (regression since last discover)"
        elif live_count == 0:
            detail = "parser found nothing"
        else:
            detail = "registry stale — run discover"
        return {
            "id": "commands-discovered",
            "status": "warning",
            "detail": detail,
            "registry_count": registry_count,
            "live_count": live_count,
        }
    return {
        "id": "commands-discovered",
        "status": "ok",
        "registry_count": registry_count,
        "live_count": live_count,
    }


def _check_json_flag(help_text: str | None) -> dict[str, Any]:
    if help_text is None:
        return {
            "id": "json-flag-advertised",
            "status": "skipped",
            "detail": "help-parses failed or skipped",
        }
    if "--json" in help_text:
        return {"id": "json-flag-advertised", "status": "ok"}
    return {
        "id": "json-flag-advertised",
        "status": "info",
        "detail": "top-level --help does not advertise --json",
    }


def _check_subcommands_audit(
    entry: dict[str, Any], typer_duo_path: str | None
) -> dict[str, Any]:
    """Run `typer-duo audit <path> --json` and roll the result into a check."""
    if typer_duo_path is None:
        return {
            "id": "subcommands-audit",
            "status": "skipped",
            "detail": "typer-duo not found on PATH",
        }

    path = entry.get("path")
    if not path:
        return {
            "id": "subcommands-audit",
            "status": "skipped",
            "detail": "registry entry has no path",
        }

    if entry.get("runtime") == "node":
        return {
            "id": "subcommands-audit",
            "status": "skipped",
            "detail": "node runtime — typer-duo is python-only",
        }

    try:
        result = subprocess.run(
            [typer_duo_path, "audit", path, "--json"],
            capture_output=True,
            text=True,
            timeout=_AUDIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {
            "id": "subcommands-audit",
            "status": "warning",
            "detail": f"typer-duo audit did not return within {_AUDIT_TIMEOUT}s",
        }
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        return {"id": "subcommands-audit", "status": "error", "detail": str(exc)}

    raw = result.stdout.strip()
    if not raw:
        detail = result.stderr.strip() or "typer-duo audit produced no output"
        return {"id": "subcommands-audit", "status": "error", "detail": detail}

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {
            "id": "subcommands-audit",
            "status": "error",
            "detail": f"could not parse typer-duo output: {exc}",
        }

    if "error" in payload and "summary" not in payload:
        return {
            "id": "subcommands-audit",
            "status": "info",
            "detail": str(payload.get("error")),
        }

    summary = payload.get("summary") or {}
    findings = payload.get("findings") or []
    severity_max = summary.get("severity_max")

    if severity_max == "error":
        status = "error"
    elif severity_max == "warning":
        status = "warning"
    elif findings:
        status = "info"
    else:
        status = "ok"

    return {
        "id": "subcommands-audit",
        "status": status,
        "score": summary.get("score"),
        "commands_total": summary.get("commands_total"),
        "commands_with_json": summary.get("commands_with_json"),
        "findings_count": len(findings),
        "severity_max": severity_max,
    }


def _check_venv_present(entry: dict[str, Any]) -> dict[str, Any] | None:
    venv = entry.get("venv")
    path = entry.get("path")
    if not venv or not path:
        return None
    venv_path = Path(path) / venv
    if venv_path.exists():
        return {"id": "venv-present", "status": "ok"}
    return {
        "id": "venv-present",
        "status": "warning",
        "detail": f"{venv} missing",
    }


def _rollup(checks: list[dict[str, Any]]) -> str:
    """Project status rollup: any error → error; else any warning → warning; else ok."""
    has_warning = False
    for c in checks:
        if c["status"] == "error":
            return "error"
        if c["status"] == "warning":
            has_warning = True
    return "warning" if has_warning else "ok"
