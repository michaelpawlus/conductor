# Agent Conductor

A meta-CLI that composes existing project CLIs into declarative, chainable agent workflows. Define a pipeline in YAML, and the conductor resolves dependencies, pipes `--json` between steps, and reports results.

## Why

Multiple CLI-enabled projects (`code-daily`, `beacon`, `timebox`, etc.) each produce structured JSON output. Conductor wires them together into repeatable multi-step workflows with parallel execution, dependency resolution, and structured error handling -- without writing shell scripts or ad-hoc subprocess calls.

## Design Principles

- **Thin orchestration, no intelligence** -- runs CLIs and wires JSON between them. Synthesis and decision-making stay in Claude Code sessions.
- **Convention over configuration** -- any CLI that supports `--json` and exits 0/1/2 is automatically compatible. No adapters needed.
- **YAML workflows are data, not code** -- readable by agents and humans alike.
- **Fail fast, report clearly** -- structured error output so an agent can diagnose failures without parsing stderr.

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

## Quick Start

```bash
# Discover all CLI-enabled projects in ~/projects/
conductor discover

# See what was found
conductor list

# List available workflows
conductor workflows

# Validate a workflow before running it
conductor validate morning-routine

# Run a workflow
conductor run morning-routine

# Run a single project command with conductor wrapping
conductor exec code-daily "streak show"

# Everything supports --json for agent consumption
conductor run morning-routine --json
```

## CLI Reference

| Command | Description |
|---|---|
| `conductor discover` | Scan `~/projects/` for CLI-enabled projects and write the registry |
| `conductor list` | List registered project CLIs and their commands |
| `conductor run <workflow>` | Run a named workflow (from `~/.conductor/workflows/` or built-ins) |
| `conductor run --inline <yaml>` | Run an inline YAML workflow definition |
| `conductor exec <project> <cmd>` | Run a single project command with conductor wrapping |
| `conductor workflows` | List available workflow definitions |
| `conductor validate <workflow>` | Validate a workflow definition without running it |
| `conductor doctor` | Re-validate the registry and surface broken or stale CLIs |
| `conductor history` | Show recent workflow runs and outcomes |
| `conductor history --id <N>` | Show the full result of a specific run |

All commands support `--json`. Human output goes to stderr, JSON to stdout.

`conductor doctor` re-validates every registered project without mutating the registry: it confirms each path still exists, each CLI resolves and responds to `--help`, and the stored `commands_discovered` count still matches live output. Flags include `--project NAME` to narrow scope, `--check-json` to probe whether a CLI advertises a top-level `--json` flag, and `--fix` to drop registry entries whose paths have disappeared. Exits `0` on clean/warning-only, `1` when any project errors, `2` when the registry file is missing.

## Workflow Format

```yaml
name: morning-routine
description: Collect context for daily planning

steps:
  - id: streak
    project: code-daily
    command: streak show

  - id: quests
    project: code-daily
    command: quests list --status active

  - id: weather
    project: timebox
    command: weather

  # Steps with dependencies run after their inputs complete
  - id: schedule
    project: timebox
    command: context
    depends_on: [weather]
```

### Step Fields

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes | Unique step identifier |
| `project` | string | yes | Registered CLI name from the registry |
| `command` | string | yes | Subcommand and flags to run |
| `depends_on` | list[str] | no | Step IDs that must complete first |
| `input_from` | string | no | Step ID whose JSON output is piped to stdin (implies `depends_on`) |
| `timeout` | int | no | Seconds before killing the step (default: 120) |
| `on_fail` | enum | no | `stop` (default), `skip`, or `retry:N` |
| `condition` | string | no | Jinja2 expression evaluated against prior step outputs |
| `extract` | string | no | jq-style path to extract from output (e.g., `.ideas[0].title`) |

### Execution Model

- Steps without `depends_on` run **in parallel** by default
- Steps with `depends_on` wait for all listed steps to complete
- `input_from` implies `depends_on` and pipes the upstream step's JSON output to stdin
- Cycles are detected at validation time

## Built-in Workflows

| Workflow | Description |
|---|---|
| `morning-routine` | Collect streak, quests, weather, workout, and briefing data in parallel |
| `news-and-ideas` | Digest news, then generate idea candidates |
| `job-search-pulse` | Surface companies with openings matching active quests |
| `weekly-retrospective` | Aggregate a week of coding activity and job search data |
| `pre-commit-check` | Fast sanity check before pushing |

## Output Format

```json
{
  "workflow": "morning-routine",
  "started_at": "2026-04-03T08:00:00+00:00",
  "completed_at": "2026-04-03T08:00:12+00:00",
  "duration_seconds": 12.0,
  "status": "completed",
  "steps": {
    "streak": {
      "status": "completed",
      "exit_code": 0,
      "duration_ms": 450,
      "output": { "current_streak": 47, "longest_streak": 52 }
    }
  }
}
```

## Configuration

| Path | Purpose |
|---|---|
| `~/.conductor/registry.yml` | Discovered project registry (auto-generated, manually editable) |
| `~/.conductor/workflows/` | User workflow definitions (override built-ins by name) |
| `~/.conductor/history.db` | SQLite run history |

### Manual Registry Entries

Auto-discovery covers projects with `[project.scripts]` in `pyproject.toml` or `bin` in `package.json`. For projects with non-standard entry points, add them manually to `~/.conductor/registry.yml`:

```yaml
projects:
  workout-app:
    path: /home/user/projects/workout-app
    cli: python backend/cli.py
    runtime: python
    commands_discovered: 0
```

## Development

```bash
pip install -e .
pip install pytest
pytest
```
