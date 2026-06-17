# Agent Conductor

Meta-CLI that composes existing project CLIs into declarative, chainable agent workflows.

## Architecture

- **Thin orchestration, no intelligence** — runs CLIs and wires JSON between them. Synthesis stays in Claude Code sessions.
- **Convention over configuration** — any CLI with `--json` + exit 0/1/2 is compatible.
- **YAML workflows are data** — readable by agents and humans.

## Project Layout

```
src/conductor/
  cli.py         — Typer app, all commands
  registry.py    — Project discovery and ~/.conductor/registry.yml management
  workflow.py    — Pydantic models, YAML loading, validation, cycle detection
  executor.py    — asyncio subprocess runner, dependency-based parallel dispatch
  output.py      — JSON (stdout) and Rich (stderr) formatting
  history.py     — SQLite run log at ~/.conductor/history.db
  workflows/     — Built-in starter workflow YAML files
```

## CLI Commands

```
conductor discover             # Scan ~/projects/ and write registry
conductor list [--json]        # Show registered CLIs
conductor run <workflow>       # Run a named workflow
conductor run --inline <yaml>  # Run inline YAML
conductor exec <project> <cmd> # Run single command with wrapping
conductor sweep <command>      # Fan one subcommand across the registry
                  [--filter has-command|has-cli|all]
                  [--parallel N] [--fail-fast]
                  [--continue-on-error/--no-continue-on-error]
                              # Cross-project composition primitive.
                              # Pairs registry + executor + history. Use `--`
                              # to forward flags verbatim:
                              #   conductor sweep -- doctor --json
conductor workflows [--json]   # List available workflows
conductor validate <workflow>  # Validate without running
conductor doctor [--json] [--project NAME] [--check-json]
                  [--check-subcommands] [--fix]
                              # Re-validate registry; flags broken/stale CLIs.
                              # Read-only by default; --fix drops stale paths.
                              # --check-subcommands runs `typer-duo audit` on
                              # each project for agent-readiness scoring.
conductor doctor diff --since FILE [--json] [--project NAME]
                  [--check-json] [--check-subcommands]
                              # Diff current registry health vs a prior
                              # doctor JSON snapshot. Adds a `diff` field
                              # with regressed/improved/newly_added/removed.
                              # Exits 0 even on regressions; 1 on unreadable
                              # or schema-incompatible --since; 2 on missing
                              # registry. Pairs with `typer-duo audit-all
                              # --since` for portfolio-readiness diffs.
conductor history [--json]     # Show recent runs
conductor history --id <N>     # Show full result of a specific run
```

All commands support `--json`. Human output goes to stderr, JSON to stdout.

## Config Locations

- `~/.conductor/registry.yml` — discovered project registry
- `~/.conductor/workflows/` — user workflow definitions (override built-ins)
- `~/.conductor/history.db` — SQLite run history (queryable, backs `conductor history`)
- `~/.conductor/runs.jsonl` — append-only JSONL outbox; one run envelope per line.
  Agent- and Obsidian-friendly handoff stream. Conductor emits it; downstream
  readers (a Claude Code session or scheduled trigger) synthesize it.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## Workflow YAML Schema

```yaml
name: workflow-name
description: What it does
steps:
  - id: step-id
    project: registered-cli-name
    command: subcommand --flags
    depends_on: [other-step]    # optional
    input_from: other-step      # optional, implies depends_on
    timeout: 120                # optional, seconds
    on_fail: stop               # stop | skip | retry:N
    condition: "expr"           # optional, Jinja2 expression
    extract: ".path[0].key"    # optional, jq-style path
```

## Agent Persona

When running in a Claude Code session, conductor is a **data-gathering tool**. The agent should:
1. Run `conductor run <workflow> --json` to collect structured data
2. Synthesize the JSON output into insights, summaries, or actions
3. Never modify workflows without user approval

For "one command, every project" surveys, reach for `conductor sweep` — it
is the cross-project composition primitive. Example: `conductor sweep --
doctor --json --filter has-command --json` collects each project's doctor
report into a single merged envelope. Workflows still own multi-step chained
pipelines; sweep owns the "one command, many projects" axis.
