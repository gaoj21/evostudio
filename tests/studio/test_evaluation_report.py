"""Evaluate a batch after it ran, from what it already holds."""

import json

import pytest

from backend.api import evaluation_report as er


def sample(sid, kind, event=None, flags=()):
    return json.dumps({"sample_id": sid, "type": kind,
                       "company": {"name": sid}, "label": {"event_date": event},
                       "news_quality": {"flags": list(flags)}})


def item(index, sid, kind, as_of, decision, event=None, status="success", flags=()):
    return {"index": index, "status": status, "run_id": f"r{index}",
            "inputs": {"sample_id": sid, "company": sid, "as_of": as_of,
                       "sample_json": sample(sid, kind, event, flags)},
            "_result": {"decision": json.dumps(decision)} if decision else None}


def result_of(it):
    return it.get("_result")


def d(action, level, score):
    return {"action": action, "risk_level": level, "score": score}


@pytest.fixture
def batch():
    return {"batch_id": "b1", "status": "completed_with_errors", "items": [
        # A positive caught a month out: medium, then high.
        item(0, "Acme", "positive", "2026-03-01", d("suppress", "medium", 40), event="2026-05-01"),
        item(1, "Acme", "positive", "2026-04-01", d("alert", "high", 70), event="2026-05-01"),
        # A negative kept low.
        item(2, "Beta", "negative", "2026-03-01", d("suppress", "low", 10)),
        item(3, "Beta", "negative", "2026-04-01", d("suppress", "low", 5)),
        # A noise sample the dataset flags, missed entirely.
        item(4, "Silver", "positive", "2026-04-01", d("suppress", "low", 0), event="2026-05-01",
             flags=("generic_name", "noisy")),
        # A failed step of a negative.
        item(5, "Beta", "negative", "2026-05-01", None, status="failed"),
    ]}


class TestCreditRiskMode:
    def test_it_is_recognised_from_the_records(self, batch):
        assert er.is_credit_risk(batch)
        assert not er.is_credit_risk({"items": [{"inputs": {"q": "x"}}]})

    def test_outcome_countdown_never_becomes_risk_truth(self):
        s = {"type": "positive", "label": {"event_date": "2026-05-01"}}
        assert er.expected_band(s, "2026-04-15") is None
        assert er.expected_band({"type": "negative"}, "2026-01-01") is None
        s["risk_annotations"]={"2026-04-15": {"risk_level":"high",
            "review_status":"reviewed", "evidence_ids":["doc1"]}}
        assert er.expected_band(s, "2026-04-15") == "high"
        assert er.expected_band(s, "2026-04-14") is None

    def test_detection_lead_and_false_alarms_per_company(self, batch):
        r = er.credit_risk_report(batch, result_of)
        by = {c["company"]: c for c in r["companies"]}
        assert by["Acme"]["detected"] is True
        assert by["Acme"]["first_alert"] == "2026-04-01"
        assert by["Acme"]["lead_days"] == 30
        assert by["Beta"]["false_alarm"] is None
        assert by["Silver"]["detected"] is False
        assert by["Beta"]["failed_steps"] == 1

    def test_weekly_bands_exact_and_near(self, batch):
        r = er.credit_risk_report(batch, result_of)
        by = {c["company"]: c for c in r["companies"]}
        # Acme 03-01: 61 days out → high expected, got medium → near.
        # Acme 04-01: 30 days → critical expected, got high → near.
        assert [s["match"] for s in by["Acme"]["steps"]] == [None, None]
        assert [s["match"] for s in by["Beta"]["steps"]] == [None, None, None]
        assert by["Beta"]["weeks"] == 0                     # the failed step is not judged

    def test_tallies_with_and_without_the_flagged_sample(self, batch):
        r = er.credit_risk_report(batch, result_of)
        assert r["all"]["detected"] == 1 and r["all"]["positives"] == 2
        assert r["unflagged"]["detected"] == 1 and r["unflagged"]["positives"] == 1
        assert r["unflagged"]["false_alarms"] == 0 and r["unflagged"]["negatives"] == 0
        assert r["unflagged"]["mean_lead_days"] == 30
        assert r["flagged"] == [{"company": "Silver", "flags": ["generic_name", "noisy"]}]
        assert r["failed_steps"] == 1

    def test_the_headline_is_one_line_a_list_row_can_show(self, batch):
        r = er.credit_risk_report(batch, result_of)
        assert r["headline"] == "detected 1/1 · 1 negatives unscored · lead 30d · ±1 countdown 100% · 1 flagged excluded"

    def test_positives_first_then_by_name(self, batch):
        r = er.credit_risk_report(batch, result_of)
        assert [c["company"] for c in r["companies"]] == ["Acme", "Silver", "Beta"]


class TestMetricMode:
    def test_scores_each_finished_record_against_its_label_field(self):
        batch = {"items": [
            {"index": 0, "status": "success", "inputs": {"q": "1+1", "answer": "2"}, "_result": {"out": "2"}},
            {"index": 1, "status": "success", "inputs": {"q": "2+2", "answer": "4"}, "_result": {"out": "5"}},
            {"index": 2, "status": "failed", "inputs": {"q": "x", "answer": "y"}, "_result": None},
        ]}
        r = er.metric_report(batch, result_of, "exact_match", "answer")
        assert [i["score"] for i in r["items"]] == [1.0, 0.0, None]
        assert r["summary"]["mean"] == 0.5 and r["summary"]["unscored"] == 1
        assert r["headline"].startswith("exact_match 0.500")

    def test_a_missing_label_field_says_which_fields_exist(self):
        from backend.api.evaluation import EvaluationError
        with pytest.raises(EvaluationError, match="no 'answer' field"):
            er.metric_report({"items": [{"index": 0, "status": "success", "inputs": {"q": 1}}]},
                             result_of, "exact_match", "answer")


class TestRoute:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch, batch):
        from fastapi.testclient import TestClient
        from backend.api import app as studio_app, batch as batch_module, runner
        monkeypatch.setattr(batch_module, "BATCHES_DIR", tmp_path / "batches")
        batch_module._batches.clear()
        batch_module._persist_batch({**batch, "graph_id": "g1", "created_at": "t", "source": {"type": "credit_risk"},
                                     "metric": None, "summary": None, "total": 6})
        results = {it["run_id"]: {"result": it["_result"]} for it in batch["items"]}
        monkeypatch.setattr(runner, "get_run", lambda rid: results.get(rid))
        return TestClient(studio_app.app)

    def test_one_call_evaluates_and_the_result_sticks_to_the_batch(self, client):
        res = client.post("/api/batches/b1/evaluate")
        assert res.status_code == 200, res.text
        assert res.json()["kind"] == "credit_risk"
        again = client.get("/api/batches/b1/evaluation").json()
        assert again["credit_risk"] is True
        assert again["evaluation"]["headline"] == res.json()["headline"]
        listed = client.get("/api/batches?graph_id=g1").json()
        assert listed[0]["evaluation"] == res.json()["headline"]

    def test_a_metric_can_be_asked_for_explicitly(self, client):
        res = client.post("/api/batches/b1/evaluate", json={"metric": "exact_match", "label_key": "nope"})
        assert res.status_code == 422 and "no 'nope' field" in res.text

    def test_unknown_batch(self, client):
        assert client.post("/api/batches/zzz/evaluate").status_code == 404


class TestForesight:
    def test_dates_after_the_step_are_caught_in_both_spellings(self):
        assert er.future_mentions("filed for bankruptcy in March 2025; note due 2024-11-01", "2024-10-24") == ["2024-11-01", "March 2025"]
        assert er.future_mentions("results reported in October 2024", "2024-10-24") == []   # same month is not the future
        assert er.future_mentions("hearing set for 2025-01-15", "2024-10-24") == ["2025-01-15"]
        assert er.future_mentions("nothing dated", "2024-10-24") == []

    def test_they_are_counted_per_step_and_in_the_headline(self, batch):
        texts = {0: "filed in April 2026", 1: "", 2: "", 3: "", 4: "", 5: ""}
        r = er.credit_risk_report(batch, result_of, lambda it: texts[it["index"]])
        assert r["foresight"] == [{"company": "Acme", "as_of": "2026-03-01", "mentions": ["April 2026"]}]
        assert r["headline"].endswith("1 foresight step(s)")


def test_a_slightly_broken_decision_json_still_yields_its_level():
    broken = '{"action": "alert", "risk_level": "high", " "score": 67, "rationale": "two lawsuits"}'
    assert er._decision({"decision": broken}) == {"action": "alert", "risk_level": "high", "score": 67, "rationale": "two lawsuits"}
    assert er._decision({"decision": "not json at all"}) is None


class TestLabelsFromAVersionedRelease:
    """The releases keep outcomes out of the records; the truth is looked up
    by case id, and a case nobody verified is neither caught nor a false
    alarm — it is listed and left out."""

    def release_item(self, index, cid, as_of, decision, status="success"):
        it = item(index, cid, "positive", as_of, decision, status=status)
        raw = json.loads(it["inputs"]["sample_json"])
        for k in ("type", "label", "news_quality"):
            raw.pop(k, None)
        it["inputs"]["sample_json"] = json.dumps(raw)
        return it

    @pytest.fixture
    def release_batch(self):
        return {"batch_id": "b2", "status": "succeeded", "source": {"dataset": "rel-1"}, "items": [
            self.release_item(0, "case-a", "2026-03-01", d("alert", "high", 70)),
            self.release_item(1, "case-a", "2026-04-01", d("alert", "critical", 90)),
            self.release_item(2, "case-u", "2026-03-01", d("alert", "high", 70)),
            self.release_item(3, "case-x", "2026-03-01", d("suppress", "low", 5)),
        ]}

    def labels(self, cid):
        return {"case-a": {"type": "positive", "event_date": "2026-05-01", "event": "chapter_11"},
                "case-u": {"type": "unverified", "reason": "negative_followup_insufficient"}}.get(cid)

    def test_positives_come_from_the_outcome_and_unverified_are_listed_not_scored(self, release_batch):
        r = er.credit_risk_report(release_batch, result_of, label_of=self.labels)
        by = {c["sample_id"]: c for c in r["companies"]}
        assert by["case-a"]["type"] == "positive" and by["case-a"]["lead_days"] == 61
        assert by["case-u"]["type"] == "unverified" and by["case-u"]["false_alarm"] is None
        assert by["case-x"]["unverified_reason"] == "no outcome record"
        assert r["all"]["positives"] == 1 and r["all"]["negatives"] == 0 and r["all"]["unverified"] == 2
        assert [u["company"] for u in r["unverified"]] == ["case-u", "case-x"]
        assert r["no_ground_truth"] is False
        assert "2 unverified excluded" in r["headline"]

    def test_countdown_is_reported_beside_not_instead_of_reviewed_labels(self, release_batch):
        r = er.credit_risk_report(release_batch, result_of, label_of=self.labels)
        a = next(c for c in r["companies"] if c["sample_id"] == "case-a")
        assert [s["countdown"] for s in a["steps"]] == ["high", "critical"]
        assert [s["expected"] for s in a["steps"]] == [None, None]     # no reviewed day labels
        assert r["all"]["countdown_near_rate"] == 1.0 and r["all"]["near_rate"] is None

    def test_a_batch_with_no_truth_at_all_says_so_instead_of_zeros(self, release_batch):
        r = er.credit_risk_report(release_batch, result_of, label_of=lambda cid: None)
        assert r["no_ground_truth"] is True
        assert r["headline"] == "no ground truth: 3 companies without a verified outcome"

    def test_the_route_looks_the_release_up_from_the_batch_source(self, tmp_path, monkeypatch, release_batch):
        from fastapi.testclient import TestClient
        from backend.api import app as studio_app, batch as batch_module, runner, datasets
        monkeypatch.setattr(batch_module, "BATCHES_DIR", tmp_path / "batches")
        batch_module._batches.clear()
        batch_module._persist_batch({**release_batch, "graph_id": "g1", "created_at": "t", "metric": None,
                                     "summary": None, "total": 4})
        results = {it["run_id"]: {"result": it["_result"]} for it in release_batch["items"]}
        monkeypatch.setattr(runner, "get_run", lambda rid: results.get(rid))
        asked = []
        monkeypatch.setattr(datasets, "evaluation_labels",
                            lambda ds: asked.append(ds) or {"case-a": self.labels("case-a"), "case-u": self.labels("case-u")})
        res = TestClient(studio_app.app).post("/api/batches/b2/evaluate")
        assert res.status_code == 200, res.text
        assert asked == ["rel-1"]
        assert res.json()["all"]["positives"] == 1


class TestEvaluationLabelRules:
    def outcome(self, **kw):
        base = {"outcome_review_status": "event_verified", "event_type": "chapter_11",
                "reviewed_event": {"scope": "registrant_and_subsidiaries", "event_date": "2026-05-01"}}
        base.update(kw)
        return base

    case = {"case_id": "c", "window": {"start": "2025-11-01", "end": "2026-04-30"}}

    def test_a_verified_registrant_insolvency_after_the_window_is_positive(self):
        from backend.api.datasets import evaluation_label
        lab = evaluation_label(self.outcome(), self.case)
        assert lab["type"] == "positive" and lab["event_date"] == "2026-05-01"
        assert evaluation_label(self.outcome(event_type="assignment_for_benefit_of_creditors"), self.case)["type"] == "positive"

    def test_everything_else_is_unverified_with_a_reason(self):
        from backend.api.datasets import evaluation_label
        cases = [
            self.outcome(outcome_review_status="negative_followup_insufficient"),
            self.outcome(event_type="refinancing"),
            self.outcome(reviewed_event={"scope": "subsidiaries_only", "event_date": "2026-05-01"}),
            self.outcome(reviewed_event={"scope": "registrant", "event_date": "2026-03-01"}),
        ]
        reasons = [evaluation_label(o, self.case) for o in cases]
        assert all(r["type"] == "unverified" for r in reasons)
        assert "negative_followup_insufficient" in reasons[0]["reason"]
        assert "refinancing" in reasons[1]["reason"]
        assert "subsidiaries_only" in reasons[2]["reason"]
        assert "inside" in reasons[3]["reason"]

    def test_a_real_release_yields_positives_and_no_negatives(self):
        from backend.api import datasets
        labels = datasets.evaluation_labels("2026-09-10-random-dev-test-v1")
        if not labels:
            pytest.skip("release not on this machine")
        kinds = {v["type"] for v in labels.values()}
        assert kinds <= {"positive", "unverified"} and "positive" in kinds
