"""Tests for executor utilities and logic."""

import asyncio

import pytest

from conductor.executor import (
    StepResult,
    evaluate_condition,
    extract_path,
    _should_run,
)
from conductor.workflow import Step


class TestExtractPath:
    def test_simple_key(self):
        assert extract_path({"name": "Alice"}, ".name") == "Alice"

    def test_nested_key(self):
        data = {"user": {"name": "Alice"}}
        assert extract_path(data, ".user.name") == "Alice"

    def test_array_index(self):
        data = {"items": ["a", "b", "c"]}
        assert extract_path(data, ".items[1]") == "b"

    def test_nested_array(self):
        data = {"ideas": [{"title": "First"}, {"title": "Second"}]}
        assert extract_path(data, ".ideas[0].title") == "First"

    def test_without_leading_dot(self):
        assert extract_path({"x": 1}, "x") == 1

    def test_missing_key_raises(self):
        with pytest.raises(KeyError):
            extract_path({"a": 1}, ".b")

    def test_index_out_of_range_raises(self):
        with pytest.raises(IndexError):
            extract_path({"a": [1]}, ".a[5]")


class TestEvaluateCondition:
    def test_true_condition(self):
        results = {"weather": StepResult(status="completed", output={"temp_f": 72})}
        assert evaluate_condition("steps.weather.output.temp_f > 50", results) is True

    def test_false_condition(self):
        results = {"weather": StepResult(status="completed", output={"temp_f": 30})}
        assert evaluate_condition("steps.weather.output.temp_f > 50", results) is False

    def test_status_check(self):
        results = {"s1": StepResult(status="completed")}
        assert (
            evaluate_condition("steps.s1.status == 'completed'", results) is True
        )

    def test_jinja_brackets_accepted(self):
        results = {"s1": StepResult(status="completed")}
        assert (
            evaluate_condition(
                "{{ steps.s1.status == 'completed' }}", results
            )
            is True
        )

    def test_bad_expression_returns_false(self):
        assert evaluate_condition("undefined_var.foo > 1", {}) is False


class TestShouldRun:
    def _make_step(self, id: str, depends_on: list[str] | None = None, on_fail: str = "stop"):
        return Step(id=id, project="p", command="cmd", depends_on=depends_on or [], on_fail=on_fail)

    def test_no_dependencies(self):
        step = self._make_step("a")
        assert _should_run(step, {}, {}) is True

    def test_dependency_completed(self):
        step = self._make_step("b", depends_on=["a"])
        results = {"a": StepResult(status="completed")}
        step_map = {"a": self._make_step("a"), "b": step}
        assert _should_run(step, results, step_map) is True

    def test_dependency_failed_with_stop(self):
        step = self._make_step("b", depends_on=["a"])
        dep = self._make_step("a", on_fail="stop")
        results = {"a": StepResult(status="failed")}
        step_map = {"a": dep, "b": step}
        assert _should_run(step, results, step_map) is False

    def test_dependency_failed_with_skip(self):
        step = self._make_step("b", depends_on=["a"])
        dep = self._make_step("a", on_fail="skip")
        results = {"a": StepResult(status="failed")}
        step_map = {"a": dep, "b": step}
        assert _should_run(step, results, step_map) is True

    def test_dependency_not_yet_complete(self):
        step = self._make_step("b", depends_on=["a"])
        assert _should_run(step, {}, {"a": self._make_step("a")}) is False
