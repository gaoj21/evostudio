"""Helpers of the credit-risk monitoring report (project evaluator code)."""
from projects.credit_risk.evaluators import monitoring_report as report


def test_dates_after_the_step_are_caught_in_both_spellings():
    assert report.future_mentions("filed for bankruptcy in March 2025; note due 2024-11-01", "2024-10-24") == ["2024-11-01", "March 2025"]
    assert report.future_mentions("results reported in October 2024", "2024-10-24") == []   # same month is not the future
    assert report.future_mentions("hearing set for 2025-01-15", "2024-10-24") == ["2025-01-15"]
    assert report.future_mentions("nothing dated", "2024-10-24") == []


def test_a_slightly_broken_decision_json_still_yields_its_level():
    broken = '{"action": "alert", "risk_level": "high", " "score": 67, "rationale": "two lawsuits"}'
    assert report._decision(broken) == {"action": "alert", "risk_level": "high", "score": 67.0, "rationale": "two lawsuits"}
    assert report._decision("not json at all") is None
