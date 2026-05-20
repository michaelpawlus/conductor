"""Tests for built-in workflows shipped under src/conductor/workflows/."""

from conductor.workflow import load_workflow, validate_workflow


def test_portfolio_pulse_loads():
    wf = load_workflow("portfolio-pulse")
    assert wf.name == "portfolio-pulse"
    assert len(wf.steps) == 4
    assert {s.id for s in wf.steps} == {
        "agent-readiness",
        "activity",
        "shippability",
        "cli-ergonomics",
    }
    for step in wf.steps:
        assert step.depends_on == []
        assert step.input_from is None


def test_portfolio_pulse_parallel_safe():
    wf = load_workflow("portfolio-pulse")
    assert wf.parallel_default is True
    for step in wf.steps:
        assert step.depends_on == []


def test_portfolio_pulse_projects_present_in_demo_registry():
    wf = load_workflow("portfolio-pulse")
    registry = {
        "agent-ready": {"path": "/fake/agent-ready", "bin": "agent-ready"},
        "portfolio-audit": {"path": "/fake/portfolio-audit", "bin": "portfolio-audit"},
        "code-daily": {"path": "/fake/code-daily", "bin": "code-daily"},
        "typer-duo": {"path": "/fake/typer-duo", "bin": "typer-duo"},
    }
    issues = validate_workflow(wf, registry)
    assert issues == []
