"""Token accounting: what a provider reported, never what we assumed.

The distinction that matters is between "this call used no tokens" and "this
provider told us nothing". A zero for the second is a silent undercount in
every total and every price downstream, so `normalize_usage` returns None.
"""

import pytest

from llm import LLMResult, LLMUsage, UsageTracker
from llm.types import normalize_usage


# ---------------------------------------------------------------------------
# normalization
# ---------------------------------------------------------------------------

def test_openai_style_names_are_understood():
    usage = normalize_usage({"prompt_tokens": 100, "completion_tokens": 20,
                             "total_tokens": 120})
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (100, 20, 120)


def test_anthropic_style_names_are_understood():
    usage = normalize_usage({"input_tokens": 7, "output_tokens": 3})
    assert (usage.input_tokens, usage.output_tokens) == (7, 3)
    assert usage.total_tokens == 10          # derived when not reported


def test_a_missing_payload_is_none_and_not_a_row_of_zeros():
    assert normalize_usage(None) is None
    assert normalize_usage({}) is None
    assert normalize_usage("usage: lots") is None
    # Half a payload is still unusable: one known count and one guess is a
    # wrong total, so nothing is reported.
    assert normalize_usage({"prompt_tokens": 10}) is None
    assert normalize_usage({"completion_tokens": 10}) is None


def test_an_object_that_is_not_a_dict_is_unwrapped():
    class Reported:
        def model_dump(self):
            return {"prompt_tokens": 4, "completion_tokens": 1}

    class Plain:
        def __init__(self):
            self.prompt_tokens = 4
            self.completion_tokens = 1

    for reported in (Reported(), Plain()):
        usage = normalize_usage(reported)
        assert (usage.input_tokens, usage.output_tokens) == (4, 1)


def test_cache_reads_and_reasoning_are_read_from_either_shape():
    modern = normalize_usage({
        "input_tokens": 1000, "output_tokens": 50,
        "input_token_details": {"cache_read": 800},
        "output_token_details": {"reasoning": 30},
    })
    legacy = normalize_usage({
        "prompt_tokens": 1000, "completion_tokens": 50,
        "prompt_tokens_details": {"cached_tokens": 800},
        "completion_tokens_details": {"reasoning_tokens": 30},
    })
    for usage in (modern, legacy):
        assert usage.cache_read_tokens == 800
        assert usage.reasoning_tokens == 30
        assert usage.uncached_input_tokens == 200


def test_nonsense_counts_are_not_propagated():
    usage = normalize_usage({"prompt_tokens": 10, "completion_tokens": 2,
                             "total_tokens": 3,           # less than the parts
                             "input_token_details": {"cache_read": 999}})
    assert usage.total_tokens == 12                        # recomputed
    assert usage.cache_read_tokens == 10                   # capped at the input
    assert usage.uncached_input_tokens == 0
    # A boolean is not a count, however much it looks like 1.
    assert normalize_usage({"prompt_tokens": True, "completion_tokens": 2}) is None


# ---------------------------------------------------------------------------
# UsageTracker
# ---------------------------------------------------------------------------

def test_totals_add_up_across_calls():
    tracker = UsageTracker()
    tracker.add(LLMResult(content="a", usage=LLMUsage(10, 2, 12)))
    tracker.add(LLMUsage(5, 1, 6))
    totals = tracker.totals
    assert (totals.input_tokens, totals.output_tokens, totals.total_tokens) == (15, 3, 18)
    assert tracker.snapshot()["token_calls"] == 2


def test_a_result_without_usage_is_not_counted_as_a_call():
    tracker = UsageTracker()
    assert tracker.add(LLMResult(content="a", usage=None)) is False
    assert tracker.add(None) is False
    assert tracker.add("42") is False
    assert tracker.snapshot()["token_calls"] == 0
    assert tracker.totals.input_tokens == 0


def test_cached_input_is_charged_at_the_cached_ratio():
    tracker = UsageTracker(input_price_per_1m=10.0, output_price_per_1m=100.0,
                           cached_input_ratio=0.1)
    # 1M input of which 500k was a cache read: 500k at full price, 500k at 10%.
    cost = tracker.token_cost(LLMUsage(input_tokens=1_000_000, output_tokens=0,
                                       total_tokens=1_000_000,
                                       cache_read_tokens=500_000))
    assert cost == pytest.approx(500_000 / 1e6 * 10 + 500_000 * 0.1 / 1e6 * 10)
    assert cost == pytest.approx(5.5)


def test_prices_come_from_the_environment_when_no_price_was_passed(monkeypatch):
    monkeypatch.setenv("EVO_TOKEN_INPUT_PRICE_PER_1M", "3")
    monkeypatch.setenv("EVO_TOKEN_OUTPUT_PRICE_PER_1M", "7")
    monkeypatch.setenv("EVO_TOKEN_CACHED_INPUT_RATIO", "0.5")
    tracker = UsageTracker()
    assert (tracker.input_price_per_1m, tracker.output_price_per_1m) == (3.0, 7.0)
    assert tracker.cached_input_ratio == 0.5
    # A price that is not a number leaves the default standing rather than
    # crashing a run over an environment typo.
    monkeypatch.setenv("EVO_TOKEN_INPUT_PRICE_PER_1M", "cheap")
    assert UsageTracker().input_price_per_1m == 2.50


def test_the_snapshot_reports_averages_and_the_combined_cost():
    tracker = UsageTracker(input_price_per_1m=1.0, output_price_per_1m=1.0,
                           web_search_price_per_1k=10.0, cached_input_ratio=0.0)
    tracker.add(LLMUsage(1_000_000, 1_000_000, 2_000_000))
    tracker.add_web_search(2)
    snapshot = tracker.snapshot()
    assert snapshot["total_token_cost"] == pytest.approx(2.0)
    assert snapshot["web_search_actions"] == 2
    assert snapshot["total_cost"] == pytest.approx(2.0 + 2 / 1000 * 10)
    assert snapshot["avg_total_tokens_per_call"] == 2_000_000


def test_an_empty_tracker_reports_no_averages_rather_than_zeroes():
    snapshot = UsageTracker().snapshot()
    assert snapshot["token_calls"] == 0
    assert snapshot["avg_input_tokens_per_call"] is None
    assert snapshot["avg_token_cost_per_call"] is None
