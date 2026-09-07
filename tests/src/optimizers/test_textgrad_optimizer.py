"""Regression tests for TextGradOptimizer's graph selection and constraint handling.

These cover three bugs found while running a real TextGrad optimization:

1. Graphs were compared by the mean of *all* metric values, so a benchmark
   reporting anything outside [0, 1] (latency, token/character counts) silently
   dominated the comparison and the wrong graph was selected.
2. `_init_textgrad` extended the module-level OPTIMIZER_CONSTRAINTS list in
   place, leaking custom constraints into every other optimizer in the process.
3. The coding-only identifier constraint was applied to every task, so
   non-coding prompts gained an instruction about matching identifiers.
"""

import pytest

from evoagentx.models import LiteLLM, LiteLLMConfig
from evoagentx.optimizers import TextGradOptimizer
from evoagentx.prompts.optimizers.textgrad_optimizer import (
    CODING_OPTIMIZER_CONSTRAINT,
    OPTIMIZER_CONSTRAINTS,
)


def _make_graph():
    from evoagentx.prompts import StringTemplate
    from evoagentx.workflow.workflow_graph import SequentialWorkFlowGraph

    return SequentialWorkFlowGraph(goal="Review the code.", tasks=[{
        "name": "review",
        "description": "Review the code.",
        "inputs": [{"name": "code", "type": "str", "required": True, "description": "The code."}],
        "outputs": [{"name": "review", "type": "str", "required": True, "description": "The review."}],
        "system_prompt": "You are a code reviewer.",
        "prompt_template": StringTemplate(instruction="Review the following code:\n{code}"),
        "parse_mode": "str",
    }])


@pytest.fixture
def captured_warnings():
    """Collect loguru warnings (the project logs through loguru, not stdlib logging)."""
    from evoagentx.core.logging import logger

    messages = []
    sink_id = logger.add(lambda message: messages.append(message), level="WARNING")
    yield messages
    logger.remove(sink_id)


def _make_optimizer(**kwargs):
    llm = LiteLLM(config=LiteLLMConfig(model="deepseek/deepseek-v4-flash", deepseek_key="fake"))
    return TextGradOptimizer(graph=_make_graph(), executor_llm=llm, optimizer_llm=llm, **kwargs)


class TestObjectiveMetric:
    def test_named_metric_is_used(self):
        optimizer = _make_optimizer(objective_metric="accuracy")
        assert optimizer._score({"accuracy": 0.875, "chars": 886.0}) == 0.875

    def test_callable_metric_is_used(self):
        optimizer = _make_optimizer(objective_metric=lambda m: m["f1"] - m["latency_ms"] / 10000)
        assert optimizer._score({"f1": 0.9, "latency_ms": 1000.0}) == pytest.approx(0.8)

    def test_unknown_metric_name_raises(self):
        optimizer = _make_optimizer(objective_metric="solve_rate")
        with pytest.raises(ValueError, match="solve_rate"):
            optimizer._score({"accuracy": 1.0})

    def test_default_averages_all_metrics(self):
        optimizer = _make_optimizer()
        assert optimizer._score({"em": 1.0, "f1": 0.5}) == pytest.approx(0.75)

    def test_default_warns_on_unnormalized_metric(self, captured_warnings):
        optimizer = _make_optimizer()
        optimizer._score({"accuracy": 0.875, "chars": 886.0})
        assert any("chars" in str(m) for m in captured_warnings)

    def test_named_metric_does_not_warn(self, captured_warnings):
        optimizer = _make_optimizer(objective_metric="accuracy")
        optimizer._score({"accuracy": 0.875, "chars": 886.0})
        assert not captured_warnings


class TestBestGraphSelection:
    """The numbers below are the metrics from the real run that exposed the bug:
    step 2 had the highest accuracy but the smallest character count, so the
    mean-of-all-metrics comparison ranked it last and picked the most verbose
    graph instead."""

    SNAPSHOT_METRICS = [
        {"accuracy": 0.625, "chars": 1713.0},
        {"accuracy": 0.875, "chars": 886.0},   # genuinely the best
        {"accuracy": 0.750, "chars": 2800.0},  # wins on mean-of-all only
    ]

    def _optimizer_with_snapshots(self, **kwargs):
        optimizer = _make_optimizer(**kwargs)
        for metrics in self.SNAPSHOT_METRICS:
            optimizer.log_snapshot(_make_graph(), metrics)
        return optimizer

    def test_objective_metric_selects_the_best_graph(self):
        optimizer = self._optimizer_with_snapshots(objective_metric="accuracy")
        _, metrics = optimizer._select_graph_with_highest_score(return_metrics=True)
        assert metrics["accuracy"] == 0.875

    def test_mean_of_all_metrics_is_dominated_by_the_unnormalized_one(self):
        # Documents *why* objective_metric exists: without it the char count decides.
        optimizer = self._optimizer_with_snapshots()
        _, metrics = optimizer._select_graph_with_highest_score(return_metrics=True)
        assert metrics["chars"] == 2800.0

    def test_rollback_and_selection_agree(self):
        optimizer = self._optimizer_with_snapshots(objective_metric="accuracy")
        scores = [optimizer._score(m) for m in self.SNAPSHOT_METRICS]
        _, metrics = optimizer._select_graph_with_highest_score(return_metrics=True)
        assert optimizer._score(metrics) == max(scores)


class TestConstraints:
    def test_module_level_constraints_are_not_mutated(self):
        before = list(OPTIMIZER_CONSTRAINTS)

        first = _make_optimizer(constraints=["CONSTRAINT_A"])
        first._init_textgrad(dataset=None, use_answers=False)
        second = _make_optimizer(constraints=["CONSTRAINT_B"])
        second._init_textgrad(dataset=None, use_answers=False)

        assert OPTIMIZER_CONSTRAINTS == before
        # and no cross-contamination between the two optimizers
        assert "CONSTRAINT_A" in first.textgrad_optimizer.constraints
        assert "CONSTRAINT_A" not in second.textgrad_optimizer.constraints
        assert "CONSTRAINT_B" in second.textgrad_optimizer.constraints

    def test_repeated_optimize_does_not_accumulate_constraints(self):
        optimizer = _make_optimizer(constraints=["CONSTRAINT_A"])
        optimizer._init_textgrad(dataset=None, use_answers=False)
        first_count = optimizer.textgrad_optimizer.constraints.count("CONSTRAINT_A")
        optimizer._init_textgrad(dataset=None, use_answers=False)
        second_count = optimizer.textgrad_optimizer.constraints.count("CONSTRAINT_A")
        assert first_count == second_count == 1

    def test_coding_constraint_is_skipped_for_non_coding_benchmarks(self):
        from evoagentx.skills import ListBenchmark

        optimizer = _make_optimizer()
        benchmark = ListBenchmark("toy", [{"code": "x = 1", "label": "fine"}])
        optimizer._init_textgrad(dataset=benchmark, use_answers=True)
        assert CODING_OPTIMIZER_CONSTRAINT not in optimizer.textgrad_optimizer.constraints

    def test_coding_constraint_is_applied_for_coding_benchmarks(self):
        from evoagentx.benchmark.benchmark import CodingBenchmark

        class ToyCodingBenchmark(CodingBenchmark):
            def __init__(self):
                super().__init__(name="toy_coding", path="", mode="all")

            def _load_data(self):
                self._train_data = self._dev_data = self._test_data = []

            def _get_id(self, example):
                return example["task_id"]

            def _get_label(self, example):
                return example

            def evaluate(self, prediction, label):
                return {"pass@1": 0.0}

            def check_solution(self, *args, **kwargs):
                return None

        optimizer = _make_optimizer()
        optimizer._init_textgrad(dataset=ToyCodingBenchmark(), use_answers=True)
        assert CODING_OPTIMIZER_CONSTRAINT in optimizer.textgrad_optimizer.constraints


class TestLabelValidation:
    def test_non_string_label_raises_a_clear_error(self):
        from evoagentx.skills import ListBenchmark

        optimizer = _make_optimizer()
        benchmark = ListBenchmark("toy", [{"code": "x = 1", "label": ["a", "b"]}])
        optimizer._init_textgrad(dataset=benchmark, use_answers=True)

        # A list label used to slip past both branches and fail deep inside
        # TextGrad with "'list' object has no attribute 'set_role_description'".
        with pytest.raises(ValueError, match="Label must be a string"):
            optimizer.step(
                inputs=[{"code": "x = 1"}],
                labels=[["a", "b"]],
                dataset=benchmark,
                use_answers=True,
            )
