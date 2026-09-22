"""Which outputs a person checks: a rule saved on the workflow, not a domain.

The rule (graph["review"]) names the node, the score field and its band, an
optional precondition, a flag field that always routes, the fields shown and
the two outcome labels. Without a rule only an explicit review flag routes.
"""

import json

import pytest

from backend.api import review


@pytest.fixture
def reviews(tmp_path, monkeypatch):
    monkeypatch.setattr(review, "REVIEWS_DIR", tmp_path / "reviews")
    monkeypatch.setattr(review, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(review, "BATCHES_DIR", tmp_path / "batches")
    return review


RULE = {"node": "grade", "score_field": "confidence", "range": [40, 60],
        "when": {"field": "decision", "equals": "escalate"},
        "flag_field": "needs_human", "show": ["decision", "confidence"],
        "approve_label": "escalate", "reject_label": "close"}


def run_state(outputs: dict, run_id="r1"):
    """A finished run whose node `grade` produced `outputs` (JSON text)."""
    return {"run_id": run_id, "graph_id": "g",
            "node_outputs": {name: {"out": json.dumps(value) if not isinstance(value, str) else value}
                             for name, value in outputs.items()}}


class TestTheBand:
    def test_a_score_inside_the_band_is_routed(self, reviews):
        made = reviews.maybe_create_review(
            run_state({"grade": {"decision": "escalate", "confidence": 50, "notes": "n"}}),
            rule=RULE)
        assert made is not None
        assert made["node"] == "grade" and made["score"] == 50
        assert made["fields"] == {"decision": "escalate", "confidence": 50}
        assert made["output"]["notes"] == "n"
        assert (made["approve_label"], made["reject_label"]) == ("escalate", "close")
        assert made["zone"] == [40.0, 60.0]
        assert reviews.get_review(made["review_id"])["status"] == "pending"

    def test_edges_of_the_band_are_inside(self, reviews):
        for score in (40, 60):
            assert reviews.maybe_create_review(
                run_state({"grade": {"decision": "escalate", "confidence": score}}), rule=RULE)

    def test_a_score_outside_the_band_is_not(self, reviews):
        for score in (39.9, 61):
            assert reviews.maybe_create_review(
                run_state({"grade": {"decision": "escalate", "confidence": score}}),
                rule=RULE) is None

    def test_the_when_condition_must_hold(self, reviews):
        assert reviews.maybe_create_review(
            run_state({"grade": {"decision": "close", "confidence": 50}}), rule=RULE) is None

    def test_only_the_named_node_is_checked(self, reviews):
        assert reviews.maybe_create_review(
            run_state({"other": {"decision": "escalate", "confidence": 50}}), rule=RULE) is None

    def test_a_batch_band_replaces_the_rule_s(self, reviews):
        state = run_state({"grade": {"decision": "escalate", "confidence": 80}})
        assert reviews.maybe_create_review(state, rule=RULE) is None
        made = reviews.maybe_create_review(state, gray_zone=[70, 90], rule=RULE)
        assert made is not None and made["zone"] == [70.0, 90.0]


class TestTheFlag:
    def test_the_rule_s_flag_field_always_routes(self, reviews):
        # Outside the band and failing `when`: the flag still wins.
        made = reviews.maybe_create_review(
            run_state({"grade": {"decision": "close", "confidence": 5, "needs_human": True}}),
            rule=RULE)
        assert made is not None and made["score"] == 5

    def test_a_truthy_non_true_flag_does_not(self, reviews):
        assert reviews.maybe_create_review(
            run_state({"grade": {"decision": "close", "confidence": 5, "needs_human": "yes"}}),
            rule=RULE) is None


class TestWithoutARule:
    def test_a_score_alone_routes_nothing(self, reviews):
        # No domain default: a "score" of 50 is not a reason to ask anyone.
        assert reviews.maybe_create_review(
            run_state({"grade": {"score": 50, "action": "alert"}})) is None

    def test_review_required_routes(self, reviews):
        made = reviews.maybe_create_review(
            run_state({"grade": {"review_required": True, "verdict": "odd"}}))
        assert made is not None
        assert made["node"] == "grade" and made["fields"]["verdict"] == "odd"
        assert (made["approve_label"], made["reject_label"]) == ("approved", "rejected")
        assert made["zone"] is None


class TestFullOutputs:
    def test_a_long_output_is_still_routed(self, reviews):
        # The node display is clipped for the UI; a clipped JSON no longer
        # parses. The review reads the full outputs instead.
        long_text = "x" * 2000
        full = json.dumps({"decision": "escalate", "confidence": 45, "notes": long_text})
        assert len(full) > 300
        state = {"run_id": "r1", "graph_id": "g",
                 "node_outputs": {"grade": {"out": full}},
                 "nodes": [{"name": "grade", "output": full[:300] + "…"}]}
        made = reviews.maybe_create_review(state, rule=RULE)
        assert made is not None
        assert made["output"]["notes"] == long_text

    def test_without_full_outputs_it_falls_back_to_node_displays(self, reviews):
        state = {"run_id": "r1", "graph_id": "g",
                 "nodes": [{"name": "grade", "output": json.dumps(
                     {"decision": "escalate", "confidence": 50})}]}
        assert reviews.maybe_create_review(state, rule=RULE) is not None


class TestTheOutcome:
    @pytest.fixture
    def pending(self, reviews, monkeypatch):
        from backend.api import batch, runner
        monkeypatch.setattr(runner, "apply_review_outcome", lambda *a, **k: None)
        monkeypatch.setattr(batch, "apply_review_outcome", lambda *a, **k: None)
        return reviews.maybe_create_review(
            run_state({"grade": {"decision": "escalate", "confidence": 50}}), rule=RULE)

    def test_approve_uses_the_rule_s_label(self, reviews, pending):
        done = reviews.resolve_review(pending["review_id"], "approve", note="ok")
        assert (done["status"], done["final_action"]) == ("approved", "escalate")

    def test_reject_uses_the_rule_s_label(self, reviews, pending):
        done = reviews.resolve_review(pending["review_id"], "reject")
        assert (done["status"], done["final_action"]) == ("rejected", "close")

    def test_without_labels_the_outcome_is_approved_or_rejected(self, reviews, monkeypatch):
        from backend.api import batch, runner
        monkeypatch.setattr(runner, "apply_review_outcome", lambda *a, **k: None)
        monkeypatch.setattr(batch, "apply_review_outcome", lambda *a, **k: None)
        made = reviews.maybe_create_review(run_state({"grade": {"review_required": True}}))
        assert reviews.resolve_review(made["review_id"], "reject")["final_action"] == "rejected"

    def test_it_is_written_back_to_the_saved_run(self, reviews, pending, tmp_path):
        (tmp_path / "runs").mkdir()
        (tmp_path / "runs" / "r1.json").write_text(json.dumps({"run_id": "r1"}))
        reviews.resolve_review(pending["review_id"], "approve")
        saved = json.loads((tmp_path / "runs" / "r1.json").read_text())
        assert saved["final_action"] == "escalate" and saved["review_status"] == "approved"

    def test_a_resolved_review_cannot_be_resolved_again(self, reviews, pending):
        reviews.resolve_review(pending["review_id"], "approve")
        with pytest.raises(ValueError):
            reviews.resolve_review(pending["review_id"], "reject")


class TestSavingTheRule:
    def test_validate_normalises(self):
        rule = review.validate_rule({"range": ["35", 65], "show": [], "node": ""})
        assert rule == {"range": [35.0, 65.0], "score_field": "score"}

    @pytest.mark.parametrize("bad", [
        "not an object",
        {"range": [70, 30]},
        {"range": [1, 2, 3]},
        {"range": ["low", "high"]},
        {"when": {"field": "decision"}},
        {"show": "decision"},
        {"show": ["f"] * 13},
        {"node": 7},
    ])
    def test_a_bad_rule_is_refused(self, bad):
        with pytest.raises(ValueError):
            review.validate_rule(bad)

    def test_it_is_saved_with_the_graph(self, studio_data):
        from backend.api import graphs
        saved = graphs.save_graph("g-review", {"name": "g", "tasks": [], "edges": [],
                                               "review": RULE})
        assert saved["review"] == {**RULE, "range": [40.0, 60.0]}
        # Kept when a later save does not mention it.
        again = graphs.save_graph("g-review", {"name": "g", "tasks": [], "edges": []})
        assert again["review"] == saved["review"]
        # And cleared by an empty one.
        assert graphs.save_graph("g-review", {"name": "g", "review": {}})["review"] is None

    def test_an_invalid_rule_is_rejected_on_save(self, studio_data):
        from fastapi.testclient import TestClient
        from backend.api import app, graphs
        graphs.save_graph("g-review", {"name": "g", "tasks": [], "edges": []})
        client = TestClient(app.app)
        res = client.put("/api/graphs/g-review",
                         json={"name": "g", "tasks": [], "edges": [],
                               "review": {"range": [90, 10]}})
        assert res.status_code == 422, res.text
        assert "low <= high" in res.text
        assert graphs.load_graph("g-review").get("review") is None
