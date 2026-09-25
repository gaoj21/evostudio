"""The credit-risk report as evaluator code, run the way any dropped-in
evaluator runs: through the platform's evaluator worker, nothing special."""
import json
from pathlib import Path

import pytest

from backend.features.evaluation import evaluator_tools
from projects.credit_risk.evaluators.labels import evaluation_label

CODE = (Path(__file__).resolve().parents[2] / "projects/credit_risk/evaluators/monitoring_report.py").read_text()


def run(i, case, as_of, level, action="suppress", status="success", sample=None, note=""):
    decision = json.dumps({"action": action, "risk_level": level, "score": 50, "rationale": note})
    return {"run_id": f"r{i}", "status": status,
            "inputs": {"sample_id": case, "company": case.title(), "as_of": as_of,
                       **({"sample_json": json.dumps(sample)} if sample else {})},
            "result": {"decision": decision} if status == "success" else None,
            "nodes": [], "node_outputs": {"decide": {"decision": decision}}}


def graph(config=None, labels=None):
    entry = {"name": "report", "code": CODE, "timing": "batch",
             "metric": "detection_rate", "config": config or {}}
    if labels is not None:
        entry["_label_records"] = labels
    return {"tasks": [], "edges": [], "evaluators": [entry]}


def evaluate(runs, **kw):
    report = evaluator_tools.evaluate_runs(graph(**kw), runs, timing={"batch"})["report"]
    assert report["status"] == "success", report.get("error")
    return report


def positive(case, event, flags=()):
    return {"sample_id": case, "type": "positive", "label": {"event_date": event}, "news_quality": {"flags": list(flags)}}


def negative(case):
    return {"sample_id": case, "type": "negative", "label": {}}


class TestFromTheRecordsOwnTruth:
    def runs(self):
        return [
            run(0, "acme", "2026-03-01", "medium", sample=positive("acme", "2026-05-01")),
            run(1, "acme", "2026-04-01", "high", "alert", sample=positive("acme", "2026-05-01")),
            run(2, "beta", "2026-03-01", "low", sample=negative("beta")),
            run(3, "beta", "2026-04-01", "low", sample=negative("beta")),
            run(4, "silver", "2026-04-01", "low", sample=positive("silver", "2026-05-01", flags=("noisy",))),
            run(5, "beta", "2026-05-01", None, status="failed", sample=negative("beta")),
        ]

    def test_metrics_leave_flagged_trajectories_out(self):
        r = evaluate(self.runs())
        assert r["metrics"]["detection_rate"] == 1.0            # acme caught; silver flagged
        assert r["metrics"]["false_alarm_rate"] == 0.0
        assert r["metrics"]["mean_lead_days"] == 30
        assert r["metrics"]["failed_steps"] == 1
        assert r["details"]["all"]["positives"] == 2 and r["details"]["all"]["detected"] == 1
        assert r["details"]["flagged"] == [{"company": "Silver", "flags": ["noisy"]}]
        assert r["objective"] == {"metric": "detection_rate", "direction": "maximize"}

    def test_one_row_per_trajectory_with_its_steps(self):
        rows = {row["trajectory"]: row for row in evaluate(self.runs())["records"]}
        assert rows["acme"]["first_alert"] == "2026-04-01" and rows["acme"]["score"] == 1.0
        assert [s["countdown"] for s in rows["acme"]["steps"]] == ["high", "critical"]
        assert [s["countdown_match"] for s in rows["acme"]["steps"]] == ["near", "near"]
        assert rows["beta"]["score"] == 1.0 and rows["silver"]["score"] == 0.0
        assert evaluate(self.runs())["coverage"] == {"unit": "trajectories", "total": 3, "scored": 3, "unscored": 0}

    def test_a_future_date_in_the_reasoning_is_counted(self):
        runs = self.runs()
        runs[0]["node_outputs"]["decide"] = {"decision": "filed in June 2026"}
        r = evaluate(runs)
        assert r["metrics"]["foresight_steps"] == 1
        assert r["details"]["foresight"] == [{"company": "Acme", "as_of": "2026-03-01", "mentions": ["June 2026"]}]


class TestFromSeparateLabels:
    def test_truth_comes_from_label_records_and_unknowns_are_not_scored(self):
        runs = [run(0, "case-a", "2026-03-01", "high", "alert"), run(1, "case-u", "2026-03-01", "high", "alert"),
                run(2, "case-x", "2026-03-01", "low")]
        labels = [{"case_id": "case-a", "type": "positive", "event_date": "2026-05-01"},
                  {"case_id": "case-u", "type": "unverified", "reason": "negative_followup_insufficient"}]
        r = evaluate(runs, labels=labels)
        rows = {row["trajectory"]: row for row in r["records"]}
        assert rows["case-a"]["detected"] and rows["case-a"]["lead_days"] == 61
        assert rows["case-u"]["type"] == "unverified" and rows["case-u"]["score"] is None
        assert rows["case-x"]["unverified_reason"] == "no verified outcome for this trajectory"
        assert r["metrics"]["false_alarm_rate"] is None           # no verified negatives exist
        assert r["coverage"]["scored"] == 1 and r["coverage"]["unscored"] == 2

    def test_field_names_are_parameters(self):
        runs = [run(0, "a", "2026-04-01", "high", "alert")]
        for rec in runs:
            rec["result"] = {"verdict": rec["result"]["decision"]}
            rec["inputs"]["when"] = rec["inputs"].pop("as_of")
            rec["inputs"]["trajectory"] = rec["inputs"].pop("sample_id")
        r = evaluate(runs, config={"decision_field": "verdict", "date_field": "when", "trajectory_field": "trajectory"},
                     labels=[{"case_id": "a", "type": "positive", "event_date": "2026-05-01"}])
        assert r["metrics"]["detection_rate"] == 1.0 and r["metrics"]["mean_lead_days"] == 30


class TestReleaseLabels:
    case = {"case_id": "c", "window": {"start": "2025-11-01", "end": "2026-04-30"}}

    def outcome(self, **kw):
        base = {"outcome_review_status": "event_verified", "event_type": "chapter_11",
                "reviewed_event": {"scope": "registrant_and_subsidiaries", "event_date": "2026-05-01"}}
        return {**base, **kw}

    def test_only_a_verified_registrant_insolvency_after_the_window_is_positive(self):
        assert evaluation_label(self.outcome(), self.case)["type"] == "positive"
        for bad in (self.outcome(outcome_review_status="negative_followup_insufficient"),
                    self.outcome(event_type="refinancing"),
                    self.outcome(reviewed_event={"scope": "subsidiaries_only", "event_date": "2026-05-01"}),
                    self.outcome(reviewed_event={"scope": "registrant", "event_date": "2026-03-01"})):
            label = evaluation_label(bad, self.case)
            assert label["type"] == "unverified" and label["reason"]

    def test_a_real_release_yields_positives_and_no_negatives(self):
        from credit_risk.studio import releases as datasets
        from projects.credit_risk.evaluators.labels import release_labels
        releases = [c["id"] for c in datasets.catalog()]
        if not releases:
            pytest.skip("no release on this machine")
        labels = release_labels(releases[0])
        assert {row["type"] for row in labels} <= {"positive", "unverified"}
        assert any(row["type"] == "positive" for row in labels)
