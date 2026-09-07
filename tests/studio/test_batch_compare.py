"""Comparing two evaluations of the same workflow.

The loop this serves: change a prompt, run the set again, ask whether it got
better. A mean answers that badly — it can rise while individual records break
— so most of what is checked here is that a comparison stays honest about what
it is and is not comparing.
"""

import pytest

from studio.backend import batch_compare
def batch(bid, scored, *, metric="exact_match", created="2026-09-01T00:00:00+00:00",
          field="city"):
    """`scored` is [(input_value, score_or_None), ...]."""
    return {
        "batch_id": bid, "graph_id": "g1", "metric": metric, "created_at": created,
        "status": "completed",
        "items": [
            {"index": i, "status": "success" if score is not None else "failed",
             "inputs": {field: value}, "score": score, "run_id": f"{bid}-{i}",
             "output_summary": f"out-{value}"}
            for i, (value, score) in enumerate(scored)
        ],
    }


class TestAggregate:
    def test_it_reports_the_movement_in_the_mean(self):
        before = batch("b1", [("Lima", 0.0), ("Oslo", 1.0)])
        after = batch("b2", [("Lima", 1.0), ("Oslo", 1.0)])
        result = batch_compare.compare(before, after)

        assert result["mean_before"] == 0.5
        assert result["mean_after"] == 1.0
        assert result["delta"] == 0.5

    def test_a_mean_that_rose_can_still_hide_a_record_that_broke(self):
        # The reason this feature exists rather than a single number.
        before = batch("b1", [("a", 0.0), ("b", 0.0), ("c", 1.0)])
        after = batch("b2", [("a", 1.0), ("b", 1.0), ("c", 0.0)])
        result = batch_compare.compare(before, after)

        assert result["delta"] > 0
        assert result["regressed"] == 1
        # And the record that broke is the first thing listed.
        assert result["changes"][0]["label"] == "c"
        assert result["changes"][0]["delta"] == -1.0

    def test_it_counts_which_way_each_record_moved(self):
        before = batch("b1", [("a", 0.0), ("b", 1.0), ("c", 0.5)])
        after = batch("b2", [("a", 1.0), ("b", 0.0), ("c", 0.5)])
        result = batch_compare.compare(before, after)

        assert (result["improved"], result["regressed"], result["unchanged"]) == (1, 1, 1)


class TestAlignment:
    def test_records_are_matched_by_their_inputs_not_their_position(self):
        # Re-sampling reorders the set; matching by index would compare Lima's
        # score against Oslo's and report both as changed.
        before = batch("b1", [("Lima", 1.0), ("Oslo", 0.0)])
        after = batch("b2", [("Oslo", 0.0), ("Lima", 1.0)])
        result = batch_compare.compare(before, after)

        assert result["matched"] == 2
        assert result["improved"] == result["regressed"] == 0
        assert result["unchanged"] == 2

    def test_only_the_records_in_both_are_compared(self):
        before = batch("b1", [("Lima", 0.0), ("Oslo", 0.0), ("Rome", 0.0)])
        after = batch("b2", [("Lima", 1.0), ("Tokyo", 1.0)])
        result = batch_compare.compare(before, after)

        assert result["matched"] == 1
        assert result["delta"] == 1.0
        assert any("appear in both" in n for n in result["notes"])

    def test_the_compared_means_cover_the_shared_records_only(self):
        # The overall means differ from the compared ones; using the overall
        # pair would compare a 3-record average with a 2-record one.
        before = batch("b1", [("Lima", 0.0), ("Rome", 1.0)])
        after = batch("b2", [("Lima", 1.0), ("Tokyo", 0.0)])
        result = batch_compare.compare(before, after)

        assert result["baseline"]["mean"] == 0.5
        assert result["candidate"]["mean"] == 0.5
        assert result["mean_before"] == 0.0     # Lima alone
        assert result["mean_after"] == 1.0
        assert result["delta"] == 1.0

    def test_nothing_in_common_is_said_rather_than_shown_as_zero_change(self):
        result = batch_compare.compare(
            batch("b1", [("Lima", 1.0)]), batch("b2", [("Tokyo", 1.0)])
        )
        assert result["matched"] == 0
        assert result["delta"] is None
        assert result["changes"] == []
        assert any("No record appears in both" in n for n in result["notes"])

    def test_ambiguous_duplicates_are_left_out_and_declared(self):
        before = batch("b1", [("Lima", 1.0), ("Lima", 0.0), ("Oslo", 1.0)])
        after = batch("b2", [("Lima", 1.0), ("Oslo", 0.0)])
        result = batch_compare.compare(before, after)

        # Which of the two Limas would the comparison be against? Guessing
        # would invent a change that may not exist.
        assert result["matched"] == 1
        assert result["changes"][0]["label"] == "Oslo"
        assert any("duplicate inputs" in n for n in result["notes"])


class TestHonesty:
    def test_two_different_metrics_are_flagged_as_incomparable(self):
        result = batch_compare.compare(
            batch("b1", [("Lima", 1.0)], metric="exact_match"),
            batch("b2", [("Lima", 0.8)], metric="f1"),
        )
        assert any("different metrics" in n for n in result["notes"])

    def test_an_unscored_record_is_counted_not_treated_as_zero(self):
        before = batch("b1", [("Lima", 1.0), ("Oslo", 1.0)])
        after = batch("b2", [("Lima", None), ("Oslo", 1.0)])
        result = batch_compare.compare(before, after)

        # Scoring it as 0 would report a regression that did not happen.
        assert result["unscored"] == 1
        assert result["regressed"] == 0
        assert result["mean_after"] == 1.0

    def test_a_record_that_stopped_running_shows_both_statuses(self):
        before = batch("b1", [("Lima", 1.0)])
        after = batch("b2", [("Lima", None)])
        change = batch_compare.compare(before, after)["changes"][0]

        assert change["status_before"] == "success"
        assert change["status_after"] == "failed"
        assert change["delta"] is None

    def test_both_batches_are_identified_in_the_result(self):
        result = batch_compare.compare(
            batch("b1", [("Lima", 1.0)], created="2026-09-01T00:00:00+00:00"),
            batch("b2", [("Lima", 1.0)], created="2026-09-05T00:00:00+00:00"),
        )
        assert result["baseline"]["batch_id"] == "b1"
        assert result["candidate"]["batch_id"] == "b2"
        assert result["candidate"]["created_at"] == "2026-09-05T00:00:00+00:00"


class TestLabels:
    def test_it_prefers_a_field_a_person_would_recognise(self):
        before = {"metric": "m", "items": [
            {"index": 0, "inputs": {"cik": "827187", "company": "Sleep Number"},
             "score": 1.0, "status": "success"}]}
        after = {"metric": "m", "items": [
            {"index": 0, "inputs": {"cik": "827187", "company": "Sleep Number"},
             "score": 0.0, "status": "success"}]}
        assert batch_compare.compare(before, after)["changes"][0]["label"] == "Sleep Number"

    def test_it_falls_back_to_the_first_field(self):
        result = batch_compare.compare(
            batch("b1", [("x", 1.0)], field="ticker"),
            batch("b2", [("x", 0.0)], field="ticker"),
        )
        assert result["changes"][0]["label"] == "ticker=x"

    def test_a_record_with_no_inputs_is_still_nameable(self):
        before = {"metric": "m", "items": [
            {"index": 7, "inputs": {}, "score": 1.0, "status": "success"}]}
        after = {"metric": "m", "items": [
            {"index": 7, "inputs": {}, "score": 0.0, "status": "success"}]}
        assert batch_compare.compare(before, after)["changes"][0]["label"] == "record 7"


class TestEndpoint:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from studio.backend import app as studio_app
        from studio.backend import batch as batch_module
        monkeypatch.setattr(batch_module, "BATCHES_DIR", tmp_path / "batches")
        batch_module._batches.clear()
        batch_module._threads.clear()
        batch_module._persist_batch(batch("before", [("Lima", 0.0), ("Oslo", 1.0)]))
        batch_module._persist_batch(batch("after", [("Lima", 1.0), ("Oslo", 1.0)]))
        return TestClient(studio_app.app)

    def test_it_compares_two_persisted_batches(self, client):
        res = client.get("/api/batches/after/compare", params={"baseline": "before"})
        assert res.status_code == 200
        body = res.json()
        assert body["delta"] == 0.5
        assert body["improved"] == 1

    def test_an_unknown_batch_on_either_side_is_a_404(self, client):
        assert client.get("/api/batches/nope/compare",
                          params={"baseline": "before"}).status_code == 404
        assert client.get("/api/batches/after/compare",
                          params={"baseline": "nope"}).status_code == 404

    def test_the_baseline_is_required(self, client):
        assert client.get("/api/batches/after/compare").status_code == 422
