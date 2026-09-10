"""Tests for evaluation — scoring a batch against expected answers.

An evaluation is a batch run with a metric attached, so the parts worth
pinning are the ones that decide whether a number can be trusted: the expected
answer must never reach the workflow, a metric that misbehaves must not turn
into a silent zero, and one bad score must not discard a batch of runs that
actually succeeded.

Custom metrics are ordinary custom tools taking `(prediction, label)`, which is
also what makes them exportable — the shape check is what keeps an unrelated
two-parameter tool out of the metric list.
"""

import pytest

def save_tool(code):
    """Save the tool a piece of code defines.

    Nothing but the code: a tool is a documented function, so its name,
    description and parameters all come from it.
    """
    from backend.api import custom_tools
    from backend.api import tools_registry
    return custom_tools.save_custom_tool(
        custom_tools.validate_spec({"code": code}, tools_registry.builtin_names())
    )


# A metric is an ordinary tool whose parameters happen to be (prediction, label).
HALF = ('def half(prediction: str, label: str) -> float:\n'
        '    """Score everything at one half."""\n'
        '    return 0.5\n')

DETAILED = ('def detailed(prediction: str, label: str) -> dict:\n'
            '    """Score, and report how long the prediction was."""\n'
            "    return {'score': 0.25, 'pred_len': len(prediction)}\n")

BOOM = ('def boom(prediction: str, label: str) -> float:\n'
        '    """A metric that always fails."""\n'
        "    raise ValueError('nope')\n")


class TestMetricDiscovery:
    def test_builtins_are_listed_and_not_custom(self, studio_data):
        from backend.api import evaluation
        metrics = {m["name"]: m for m in evaluation.available_metrics()}
        assert "exact_match" in metrics
        assert metrics["exact_match"]["custom"] is False
        assert metrics["exact_match"]["description"]

    def test_a_two_parameter_tool_becomes_a_metric(self, studio_data):
        from backend.api import evaluation
        save_tool(HALF)
        metrics = {m["name"]: m for m in evaluation.available_metrics()}
        assert metrics["half"]["custom"] is True

    def test_a_tool_of_another_shape_is_not_a_metric(self, studio_data):
        """Otherwise every one-argument helper would clutter the metric list."""
        from backend.api import evaluation
        save_tool('def cleaner(record: dict) -> dict:\n'
                  '    """Tidy one record."""\n'
                  '    return record\n')
        save_tool('def wrong_names(a: str, b: str) -> int:\n'
                  '    """Two arguments, but not a metric\'s."""\n'
                  '    return 1\n')
        names = {m["name"] for m in evaluation.available_metrics()}
        assert "cleaner" not in names and "wrong_names" not in names


class TestScoring:
    def test_builtin_exact_match(self, studio_data):
        from backend.api import evaluation
        assert evaluation.score_one("exact_match", "Paris", "Paris")["score"] == 1.0
        assert evaluation.score_one("exact_match", "Paris", "Rome")["score"] == 0.0

    def test_a_custom_metric_may_return_a_bare_number(self, studio_data):
        from backend.api import evaluation
        save_tool(HALF)
        assert evaluation.score_one("half", "a", "b") == {"score": 0.5}

    def test_a_custom_metric_may_return_a_bool(self, studio_data):
        from backend.api import evaluation
        save_tool('def same(prediction: str, label: str) -> bool:\n'
                  '    """Whether the prediction matches the label exactly."""\n'
                  '    return prediction == label\n')
        assert evaluation.score_one("same", "x", "x")["score"] == 1.0
        assert evaluation.score_one("same", "x", "y")["score"] == 0.0

    def test_extra_fields_of_an_object_result_are_kept(self, studio_data):
        """A metric earns its keep by explaining a score, not just giving one."""
        from backend.api import evaluation
        save_tool(DETAILED)
        scored = evaluation.score_one("detailed", "abcd", "z")
        assert scored == {"score": 0.25, "pred_len": 4}

    def test_structured_predictions_reach_the_metric_as_text(self, studio_data):
        from backend.api import evaluation
        save_tool('def echo(prediction: str, label: str) -> dict:\n'
                  '    """Score everything perfect and report what it saw."""\n'
                  "    return {'score': 1.0, 'seen': prediction}\n")
        scored = evaluation.score_one("echo", {"city": "Paris"}, "Paris")
        assert "Paris" in scored["seen"]

    def test_an_unknown_metric_says_what_is_available(self, studio_data):
        from backend.api import evaluation
        with pytest.raises(evaluation.EvaluationError) as raised:
            evaluation.score_one("nope", "a", "b")
        assert "exact_match" in str(raised.value)

    def test_an_object_without_a_score_is_refused(self, studio_data):
        from backend.api import evaluation
        save_tool('def noscore(prediction: str, label: str) -> dict:\n'
                  '    """Return an object with no score in it."""\n'
                  "    return {'grade': 'A'}\n")
        with pytest.raises(evaluation.EvaluationError) as raised:
            evaluation.score_one("noscore", "a", "b")
        assert "score" in str(raised.value)

    def test_a_non_numeric_result_is_refused(self, studio_data):
        """A string result silently coerced to 0.0 reads as 'the workflow was
        wrong' when the truth is 'the metric is broken'."""
        from backend.api import evaluation
        save_tool('def texty(prediction: str, label: str) -> str:\n'
                  '    """Return something that is not a number."""\n'
                  "    return 'great'\n")
        with pytest.raises(evaluation.EvaluationError):
            evaluation.score_one("texty", "a", "b")

    def test_a_metric_that_raises_is_reported(self, studio_data):
        from backend.api import evaluation
        save_tool(BOOM)
        with pytest.raises(evaluation.EvaluationError) as raised:
            evaluation.score_one("boom", "a", "b")
        assert "boom" in str(raised.value)


class TestLabels:
    def test_the_label_is_taken_out_of_the_inputs(self, studio_data):
        """A node declaring an input of the same name would otherwise be handed
        the answer it is being tested on."""
        from backend.api import evaluation
        inputs, labels = evaluation.split_labels(
            [{"country": "France", "answer": "Paris"}], "answer")
        assert inputs == [{"country": "France"}]
        assert labels == ["Paris"]

    def test_a_record_missing_the_field_names_it(self, studio_data):
        from backend.api import evaluation
        with pytest.raises(evaluation.EvaluationError) as raised:
            evaluation.split_labels([{"country": "France"}], "answer")
        assert "Record 1" in str(raised.value) and "country" in str(raised.value)

    def test_no_label_key_is_refused(self, studio_data):
        from backend.api import evaluation
        with pytest.raises(evaluation.EvaluationError):
            evaluation.split_labels([{"a": 1}], "")


class TestSummary:
    def test_aggregate_over_scored_items(self, studio_data):
        from backend.api import evaluation
        summary = evaluation.summarise([{"score": 1.0}, {"score": 0.5}, {"score": 0.0}])
        assert summary["scored"] == 3 and summary["total"] == 3
        assert summary["mean"] == 0.5
        assert (summary["min"], summary["max"]) == (0.0, 1.0)
        assert summary["perfect"] == 1

    def test_unscored_items_are_counted_not_treated_as_zero(self, studio_data):
        """Counting an unscored item as 0.0 quietly drags the mean down."""
        from backend.api import evaluation
        summary = evaluation.summarise([{"score": 1.0}, {"score": None}])
        assert summary["mean"] == 1.0
        assert summary["scored"] == 1 and summary["unscored"] == 1

    def test_nothing_scored_reports_no_mean(self, studio_data):
        from backend.api import evaluation
        summary = evaluation.summarise([{"score": None}])
        assert summary["mean"] is None and summary["scored"] == 0


class TestBatchScoring:
    def test_a_failing_metric_leaves_the_run_intact(self, studio_data):
        """The runs succeeded and are worth keeping; only the number is missing."""
        from backend.api import batch
        from backend.api import evaluation  # noqa: F401  (imported lazily by _score_item)

        save_tool(BOOM)
        item = {"status": "success", "label": "x", "score": None, "score_detail": None}
        batch._score_item({"metric": "boom"}, item, "prediction")

        assert item["status"] == "success"
        assert item["score"] is None
        assert "nope" in item["score_detail"]["error"]

    def test_a_successful_metric_fills_in_the_score(self, studio_data):
        from backend.api import batch
        save_tool('def detailed(prediction: str, label: str) -> dict:\n'
                  '    """Score, with a note about how close it was."""\n'
                  "    return {'score': 0.75, 'note': 'close'}\n")
        item = {"status": "success", "label": "x", "score": None, "score_detail": None}
        batch._score_item({"metric": "detailed"}, item, "prediction")

        assert item["score"] == 0.75
        assert item["score_detail"] == {"note": "close"}
