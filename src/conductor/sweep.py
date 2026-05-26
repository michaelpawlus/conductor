"""Fan a single subcommand across the registered project portfolio.

`conductor sweep "<command>"` dispatches the same command to every registered
project, gathers per-project JSON results, and rolls them up into one envelope.

This is the cross-project composition primitive: the registry already knows
which CLIs exist, the executor already runs them with the `--json` contract,
and history already records run outcomes. Sweep just wires those together.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Callable

from .executor import StepResult, _run_step
from .history import record_run
from .registry import list_project_commands
from .workflow import Step

FILTER_MODES = ("has-command", "has-cli", "all")
DEFAULT_PARALLEL = 4


def _head_subcommand(command: str) -> str | None:
    """Return the first non-flag token from `command`, or None."""
    for token in command.split():
        if not token.startswith("-"):
            return token
    return None


def _eligible_projects(
    command: str,
    registry: dict[str, dict[str, Any]],
    filter_mode: str,
) -> tuple[list[str], dict[str, str]]:
    """Decide which projects sweep should dispatch against.

    Returns (eligible_names, skip_reasons). `skip_reasons` maps each filtered-out
    project to a human-readable reason; sweep surfaces those as `status="skipped"`
    envelopes so the caller can see what was excluded and why.
    """
    if filter_mode not in FILTER_MODES:
        raise ValueError(
            f"unknown filter mode {filter_mode!r}; expected one of {FILTER_MODES}"
        )

    names = sorted(registry.keys())
    if filter_mode in ("has-cli", "all"):
        return names, {}

    head = _head_subcommand(command)
    if head is None:
        return names, {}

    eligible: list[str] = []
    skipped: dict[str, str] = {}
    for name in names:
        commands = list_project_commands({"name": name, **registry[name]})
        if head in commands:
            eligible.append(name)
        else:
            skipped[name] = (
                f"project does not expose '{head}' "
                f"(commands: {', '.join(commands) if commands else 'none discovered'})"
            )
    return eligible, skipped


async def _dispatch_one(
    project_name: str,
    command: str,
    registry: dict[str, dict[str, Any]],
    sem: asyncio.Semaphore,
    stderr_callback: Callable[[str, str], None] | None,
) -> tuple[str, StepResult]:
    async with sem:
        step = Step(id=project_name, project=project_name, command=command)
        result = await _run_step(step, registry, {}, stderr_callback)
        return project_name, result


def _envelope(project: str, command: str, result: StepResult) -> dict[str, Any]:
    env: dict[str, Any] = {
        "project": project,
        "command": command,
        "status": result.status,
        "duration_ms": result.duration_ms,
    }
    if result.exit_code is not None:
        env["exit_code"] = result.exit_code
    if result.output is not None:
        env["output"] = result.output
    if result.raw_output and result.output is None:
        env["raw_output"] = result.raw_output
    if result.error:
        env["error"] = result.error
    return env


def _skipped_envelope(project: str, command: str, reason: str) -> dict[str, Any]:
    return {
        "project": project,
        "command": command,
        "status": "skipped",
        "duration_ms": 0,
        "error": reason,
    }


def _is_success(status: str) -> bool:
    return status in ("completed", "parse_error")


async def run_sweep(
    command: str,
    registry: dict[str, dict[str, Any]],
    *,
    filter_mode: str = "has-command",
    parallel: int = DEFAULT_PARALLEL,
    continue_on_error: bool = True,
    stderr_callback: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """Fan `command` across every project in `registry` and return one envelope.

    `filter_mode` decides which projects participate:
        - ``"has-command"`` (default): probe each CLI's ``--help`` and keep only
          projects that list the head subcommand. Slower but safe for surveys.
        - ``"has-cli"``: include every registered project; let projects without
          the subcommand fail with their own exit code.
        - ``"all"``: alias for ``has-cli``; included for symmetry with the spec.

    `continue_on_error=False` cancels in-flight dispatches at the first failure
    and short-circuits with whatever has completed so far.

    Always records the run in history.db so `conductor history` covers sweeps.
    """
    started = time.monotonic()
    started_at = datetime.now(timezone.utc)

    eligible, skip_reasons = _eligible_projects(command, registry, filter_mode)

    sem = asyncio.Semaphore(max(1, parallel))
    tasks = [
        asyncio.create_task(_dispatch_one(name, command, registry, sem, stderr_callback))
        for name in eligible
    ]

    results: list[dict[str, Any]] = []
    aborted = False
    try:
        for coro in asyncio.as_completed(tasks):
            name, step_result = await coro
            results.append(_envelope(name, command, step_result))
            if not continue_on_error and not _is_success(step_result.status):
                aborted = True
                break
    finally:
        if aborted:
            for t in tasks:
                if not t.done():
                    t.cancel()
            # Drain cancellations so they don't surface as warnings.
            await asyncio.gather(*tasks, return_exceptions=True)

    completed_names = {r["project"] for r in results}
    for name in eligible:
        if name not in completed_names:
            results.append(
                {
                    "project": name,
                    "command": command,
                    "status": "skipped",
                    "duration_ms": 0,
                    "error": "fail-fast: aborted before dispatch",
                }
            )

    for name, reason in skip_reasons.items():
        results.append(_skipped_envelope(name, command, reason))

    results.sort(key=lambda r: r["project"])

    completed_at = datetime.now(timezone.utc)
    duration_seconds = round(time.monotonic() - started, 2)

    summary = {
        "total": len(results),
        "completed": sum(1 for r in results if _is_success(r["status"])),
        "failed": sum(
            1
            for r in results
            if r["status"] in ("failed", "error", "timeout")
        ),
        "skipped": sum(1 for r in results if r["status"] == "skipped"),
    }

    if summary["failed"] == 0 and summary["completed"] > 0:
        overall_status = "completed"
    elif summary["completed"] > 0:
        overall_status = "partial"
    elif summary["failed"] > 0:
        overall_status = "failed"
    else:
        overall_status = "skipped"

    envelope = {
        "workflow": f"_sweep:{command}",
        "kind": "sweep",
        "command": command,
        "filter": filter_mode,
        "parallel": parallel,
        "fail_fast": not continue_on_error,
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "duration_seconds": duration_seconds,
        "duration_ms": int(duration_seconds * 1000),
        "status": overall_status,
        "results": results,
        "summary": summary,
    }

    try:
        record_run(envelope)
    except Exception:  # noqa: BLE001  — history writes never block sweeps
        pass

    return envelope
