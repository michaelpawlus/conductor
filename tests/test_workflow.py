"""Tests for workflow parsing and validation."""

from pathlib import Path

import pytest
import yaml

from conductor.workflow import (
    Workflow,
    Step,
    load_workflow,
    parse_workflow_yaml,
    _detect_cycles,
)

FIXTURES = Path(__file__).parent / "fixtures" / "sample_workflows"


class TestStepModel:
    def test_valid_on_fail_stop(self):
        step = Step(id="s", project="p", command="cmd", on_fail="stop")
        assert step.on_fail == "stop"

    def test_valid_on_fail_skip(self):
        step = Step(id="s", project="p", command="cmd", on_fail="skip")
        assert step.on_fail == "skip"

    def test_valid_on_fail_retry(self):
        step = Step(id="s", project="p", command="cmd", on_fail="retry:3")
        assert step.on_fail == "retry:3"

    def test_invalid_on_fail(self):
        with pytest.raises(ValueError, match="Invalid on_fail"):
            Step(id="s", project="p", command="cmd", on_fail="explode")

    def test_invalid_retry_format(self):
        with pytest.raises(ValueError, match="Invalid on_fail"):
            Step(id="s", project="p", command="cmd", on_fail="retry:abc")

    def test_defaults(self):
        step = Step(id="s", project="p", command="cmd")
        assert step.depends_on == []
        assert step.input_from is None
        assert step.timeout == 120
        assert step.on_fail == "stop"
        assert step.condition is None
        assert step.extract is None


class TestWorkflowModel:
    def test_valid_parallel(self):
        wf = Workflow(
            name="test",
            steps=[
                Step(id="a", project="p", command="cmd1"),
                Step(id="b", project="p", command="cmd2"),
            ],
        )
        assert len(wf.steps) == 2

    def test_valid_dependencies(self):
        wf = Workflow(
            name="test",
            steps=[
                Step(id="a", project="p", command="cmd1"),
                Step(id="b", project="p", command="cmd2", depends_on=["a"]),
            ],
        )
        assert wf.steps[1].depends_on == ["a"]

    def test_input_from_implies_depends_on(self):
        wf = Workflow(
            name="test",
            steps=[
                Step(id="a", project="p", command="cmd1"),
                Step(id="b", project="p", command="cmd2", input_from="a"),
            ],
        )
        assert "a" in wf.steps[1].depends_on

    def test_duplicate_ids_rejected(self):
        with pytest.raises(ValueError, match="Duplicate step IDs"):
            Workflow(
                name="test",
                steps=[
                    Step(id="a", project="p", command="cmd1"),
                    Step(id="a", project="p", command="cmd2"),
                ],
            )

    def test_unknown_dependency_rejected(self):
        with pytest.raises(ValueError, match="unknown step"):
            Workflow(
                name="test",
                steps=[
                    Step(id="a", project="p", command="cmd1", depends_on=["z"]),
                ],
            )

    def test_unknown_input_from_rejected(self):
        with pytest.raises(ValueError, match="unknown step"):
            Workflow(
                name="test",
                steps=[
                    Step(id="a", project="p", command="cmd1", input_from="z"),
                ],
            )

    def test_cycle_detected(self):
        with pytest.raises(ValueError, match="Cycle detected"):
            Workflow(
                name="test",
                steps=[
                    Step(id="a", project="p", command="cmd1", depends_on=["b"]),
                    Step(id="b", project="p", command="cmd2", depends_on=["a"]),
                ],
            )


class TestLoadWorkflow:
    def test_load_from_file(self):
        wf = load_workflow(str(FIXTURES / "valid-parallel.yml"))
        assert wf.name == "valid-parallel"
        assert len(wf.steps) == 2

    def test_load_sequential(self):
        wf = load_workflow(str(FIXTURES / "valid-sequential.yml"))
        assert wf.name == "valid-sequential"
        assert wf.steps[1].depends_on == ["first"]
        assert wf.steps[1].input_from == "first"

    def test_load_cycle_fails(self):
        with pytest.raises(ValueError, match="Cycle detected"):
            load_workflow(str(FIXTURES / "cycle.yml"))

    def test_load_missing_raises(self):
        with pytest.raises(FileNotFoundError):
            load_workflow("nonexistent-workflow-xyz")

    def test_load_builtin(self):
        wf = load_workflow("morning-routine")
        assert wf.name == "morning-routine"
        assert len(wf.steps) == 7


class TestParseYaml:
    def test_inline_yaml(self):
        yaml_str = """
name: inline-test
description: test
steps:
  - id: one
    project: p
    command: cmd
"""
        wf = parse_workflow_yaml(yaml_str)
        assert wf.name == "inline-test"
        assert len(wf.steps) == 1

    def test_invalid_yaml_type(self):
        with pytest.raises(ValueError, match="mapping"):
            parse_workflow_yaml("just a string")
