"""Async workflow executor with dependency resolution and parallel dispatch."""

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable

from jinja2 import Environment

from .history import record_run
from .registry import get_cli_command
from .workflow import Step, Workflow


class StepResult:
    """Result of executing a single workflow step."""

    def __init__(
        self,
        status: str = "pending",
        exit_code: int | None = None,
        duration_ms: int = 0,
        output: Any = None,
        raw_output: str = "",
        error: str = "",
    ):
        self.status = status
        self.exit_code = exit_code
        self.duration_ms = duration_ms
        self.output = output
        self.raw_output = raw_output
        self.error = error

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"status": self.status, "duration_ms": self.duration_ms}
        if self.exit_code is not None:
            d["exit_code"] = self.exit_code
        if self.output is not None:
            d["output"] = self.output
        if self.raw_output and self.output is None:
            d["raw_output"] = self.raw_output
        if self.error:
            d["error"] = self.error
        return d


def extract_path(data: Any, path: str) -> Any:
    """Extract a value from data using a jq-style dot path.

    Supports: .key, .key.subkey, .key[0], .key[0].subkey
    """
    if path.startswith("."):
        path = path[1:]

    tokens = re.findall(r"(\w+)|\[(\d+)\]", path)
    current = data
    for key, index in tokens:
        if key:
            if isinstance(current, dict):
                current = current[key]
            else:
                current = getattr(current, key)
        elif index:
            current = current[int(index)]
    return current


def evaluate_condition(
    condition: str, step_results: dict[str, "StepResult"]
) -> bool:
    """Evaluate a Jinja2 condition expression against step results."""
    env = Environment()

    steps_ctx: dict[str, dict[str, Any]] = {}
    for step_id, result in step_results.items():
        steps_ctx[step_id] = {
            "status": result.status,
            "exit_code": result.exit_code,
            "output": result.output if result.output is not None else {},
        }

    expr = condition.strip()
    if not expr.startswith("{{"):
        expr = "{{ " + expr + " }}"

    try:
        template = env.from_string(expr)
        rendered = template.render(steps=steps_ctx)
        return rendered.strip().lower() in ("true", "1", "yes")
    except Exception:
        return False


def _should_run(
    step: Step,
    step_results: dict[str, StepResult],
    step_map: dict[str, Step],
) -> bool:
    """Check if a step should run based on dependency outcomes."""
    for dep_id in step.depends_on:
        dep_result = step_results.get(dep_id)
        if not dep_result:
            return False

        if dep_result.status in ("completed", "parse_error"):
            continue

        # Dependency didn't fully succeed — check its on_fail policy
        dep_step = step_map.get(dep_id)
        if dep_step and dep_step.on_fail == "skip":
            continue  # Downstream allowed to proceed

        return False  # stop (default) or exhausted retries → block downstream

    return True


async def _run_step(
    step: Step,
    registry: dict[str, Any],
    step_results: dict[str, StepResult],
    stderr_callback: Callable[[str, str], None] | None = None,
) -> StepResult:
    """Execute a single CLI step as a subprocess."""
    project = registry.get(step.project)
    if not project:
        return StepResult(
            status="error",
            error=f"Project '{step.project}' not found in registry",
        )

    cmd_parts, cwd = get_cli_command(project)
    cmd_parts = cmd_parts + step.command.split()

    if "--json" not in cmd_parts:
        cmd_parts.append("--json")

    # Prepare stdin if input_from is set
    stdin_data: bytes | None = None
    if step.input_from and step.input_from in step_results:
        upstream = step_results[step.input_from]
        if upstream.output is not None:
            stdin_data = json.dumps(upstream.output).encode()

    start = time.monotonic()

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd_parts,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE if stdin_data else asyncio.subprocess.DEVNULL,
            cwd=str(cwd),
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(input=stdin_data),
                timeout=step.timeout,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return StepResult(
                status="timeout",
                duration_ms=int((time.monotonic() - start) * 1000),
                error=f"Timed out after {step.timeout}s",
            )

        duration_ms = int((time.monotonic() - start) * 1000)
        raw_stdout = stdout.decode().strip() if stdout else ""
        raw_stderr = stderr.decode().strip() if stderr else ""

        if stderr_callback and raw_stderr:
            stderr_callback(step.id, raw_stderr)

        if process.returncode != 0:
            return StepResult(
                status="failed",
                exit_code=process.returncode,
                duration_ms=duration_ms,
                raw_output=raw_stdout,
                error=raw_stderr or f"Exit code {process.returncode}",
            )

        # Parse JSON output
        output = None
        if raw_stdout:
            try:
                output = json.loads(raw_stdout)
            except json.JSONDecodeError:
                return StepResult(
                    status="parse_error",
                    exit_code=0,
                    duration_ms=duration_ms,
                    raw_output=raw_stdout,
                    error="Output is not valid JSON",
                )

        # Apply extract path
        if step.extract and output is not None:
            try:
                output = extract_path(output, step.extract)
            except (KeyError, IndexError, TypeError) as e:
                return StepResult(
                    status="parse_error",
                    exit_code=0,
                    duration_ms=duration_ms,
                    output=output,
                    error=f"Extract '{step.extract}' failed: {e}",
                )

        return StepResult(
            status="completed",
            exit_code=0,
            duration_ms=duration_ms,
            output=output,
        )

    except FileNotFoundError:
        return StepResult(
            status="error",
            duration_ms=int((time.monotonic() - start) * 1000),
            error=f"CLI not found: {cmd_parts[0]}",
        )
    except Exception as e:
        return StepResult(
            status="error",
            duration_ms=int((time.monotonic() - start) * 1000),
            error=str(e),
        )


async def execute_workflow(
    workflow: Workflow,
    registry: dict[str, Any],
    stderr_callback: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """Execute a full workflow with dependency-based parallel dispatch.

    Steps without dependencies launch immediately in parallel.
    Steps with depends_on wait until all dependencies complete.
    """
    started_at = datetime.now(timezone.utc)
    step_results: dict[str, StepResult] = {}
    step_events: dict[str, asyncio.Event] = {
        step.id: asyncio.Event() for step in workflow.steps
    }
    step_map: dict[str, Step] = {step.id: step for step in workflow.steps}

    async def _run_and_signal(step: Step) -> None:
        # Wait for all dependencies to complete
        for dep_id in step.depends_on:
            await step_events[dep_id].wait()

        # Check dependency outcomes
        if not _should_run(step, step_results, step_map):
            step_results[step.id] = StepResult(
                status="skipped", error="Dependency failed"
            )
            step_events[step.id].set()
            return

        # Evaluate condition
        if step.condition:
            if not evaluate_condition(step.condition, step_results):
                step_results[step.id] = StepResult(
                    status="skipped", error="Condition not met"
                )
                step_events[step.id].set()
                return

        # Determine retry count
        max_retries = 0
        if step.on_fail.startswith("retry:"):
            max_retries = int(step.on_fail.split(":")[1])

        result = None
        for attempt in range(max_retries + 1):
            result = await _run_step(step, registry, step_results, stderr_callback)
            if result.status == "completed":
                break
            if attempt < max_retries:
                await asyncio.sleep(1)

        step_results[step.id] = result  # type: ignore[assignment]
        step_events[step.id].set()

    # Launch all steps concurrently — dependencies gate via events
    tasks = [asyncio.create_task(_run_and_signal(step)) for step in workflow.steps]
    await asyncio.gather(*tasks)

    completed_at = datetime.now(timezone.utc)
    duration = (completed_at - started_at).total_seconds()

    statuses = {r.status for r in step_results.values()}
    if statuses <= {"completed"}:
        overall_status = "completed"
    elif statuses & {"failed", "error", "timeout"}:
        overall_status = "partial"
    else:
        overall_status = "completed"

    result = {
        "workflow": workflow.name,
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "duration_seconds": round(duration, 2),
        "status": overall_status,
        "steps": {sid: sr.to_dict() for sid, sr in step_results.items()},
    }

    try:
        record_run(result)
    except Exception:
        pass  # Don't fail the workflow over history recording

    return result


async def execute_single(
    project_name: str,
    command: str,
    registry: dict[str, Any],
) -> dict[str, Any]:
    """Execute a single project command with conductor wrapping."""
    step = Step(id="exec", project=project_name, command=command)
    result = await _run_step(step, registry, {})

    return {
        "project": project_name,
        "command": command,
        "status": result.status,
        **result.to_dict(),
    }
