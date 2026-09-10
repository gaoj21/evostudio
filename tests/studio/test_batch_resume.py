"""Picking a stopped batch up where it left off.

A batch of 42 stepped records that dies at 16 — a network drop, a restart,
a Stop pressed for a reason since fixed — used to mean starting over: an
hour of model calls again, for records that were already done. Resume runs
the ones that did not finish and keeps the rest.
"""

import pytest


@pytest.fixture
def batches(tmp_path, monkeypatch):
    from backend.api import batch as batch_module
    monkeypatch.setattr(batch_module, "BATCHES_DIR", tmp_path / "batches")
    monkeypatch.setattr(batch_module, "_pause", lambda seconds: None)
    batch_module._batches.clear()
    batch_module._threads.clear()
    yield batch_module
    for batch_id in list(batch_module._threads):
        assert batch_module.wait_for(batch_id, timeout=10)


class Recorder:
    def __init__(self, outcome=None):
        self.calls, self.runs = [], {}
        self.outcome = outcome or (lambda record: {"status": "success", "result": {"d": record["sample_id"]}})

    def start_run(self, graph, record, background, gray_zone, run_id, batch_id, session_started_at):
        self.calls.append((record["sample_id"], record.get("as_of")))
        self.runs[run_id] = dict(self.outcome(record))

    def get_run(self, run_id):
        return self.runs.get(run_id)


def stopped_batch(batches, statuses):
    """A persisted batch as a stop left it: `statuses` per item, in order."""
    items = [{"index": i, "status": st, "run_id": f"r{i}" if st != "pending" else None,
              "inputs": {"sample_id": "acme", "as_of": f"2026-0{i + 1}-01"},
              "output_summary": "…" if st == "success" else None, "error": None,
              "review_status": None, "label": None, "score": 1.0 if st == "success" else None,
              "score_detail": None, "attempts": 1 if st != "pending" else 0}
             for i, st in enumerate(statuses)]
    state = {"batch_id": "b1", "graph_id": "g1", "status": "cancelled",
             "created_at": "2026-09-08T05:00:00+00:00", "source": {"type": "credit_risk"},
             "metric": None, "summary": None, "workers": 1, "total": len(items),
             "items": items, "cancelled_items": 2}
    batches._persist_batch(state)
    return state


def test_only_what_did_not_finish_is_run_again_in_order(batches, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(batches.runner, "start_run", rec.start_run)
    monkeypatch.setattr(batches.runner, "get_run", rec.get_run)
    stopped_batch(batches, ["success", "success", "cancelled", "pending", "failed"])

    outcome = batches.resume_batch("b1", {"id": "g1"})
    assert outcome == {"resumed": True, "batch_id": "b1", "remaining": 3, "kept": 2,
                       "rerun_after": 0}
    assert batches.wait_for("b1", timeout=10)

    assert [a for _, a in rec.calls] == ["2026-03-01", "2026-04-01", "2026-05-01"]
    state = batches.get_batch("b1")
    assert [i["status"] for i in state["items"]] == ["success"] * 5
    assert state["items"][0]["score"] == 1.0            # the kept ones untouched
    assert state["items"][2]["attempts"] == 2
    assert state["status"] == "succeeded"
    assert len(state["resumed_at"]) == 1


def test_it_survives_a_restart_the_batch_was_only_on_disk(batches, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(batches.runner, "start_run", rec.start_run)
    monkeypatch.setattr(batches.runner, "get_run", rec.get_run)
    stopped_batch(batches, ["success", "pending"])
    batches._batches.clear()                             # process restarted

    assert batches.resume_batch("b1", {"id": "g1"})["resumed"] is True
    assert batches.wait_for("b1", timeout=10)
    assert batches.get_batch("b1")["status"] == "succeeded"


def test_a_running_batch_is_not_resumed(batches, monkeypatch):
    state = stopped_batch(batches, ["success", "pending"])
    state["status"] = "running"
    batches._batches["b1"] = state
    assert batches.resume_batch("b1", {"id": "g1"}) == {
        "resumed": False, "reason": "batch is still running"}


def test_a_finished_batch_has_nothing_to_resume(batches):
    stopped_batch(batches, ["success", "success"])
    assert batches.resume_batch("b1", {"id": "g1"})["reason"] == "nothing left to run"


def test_another_workflow_s_batch_is_refused(batches):
    stopped_batch(batches, ["success", "pending"])
    assert batches.resume_batch("b1", {"id": "g2"})["reason"] == "batch belongs to another workflow"


def test_unknown(batches):
    assert batches.resume_batch("nope", {"id": "g1"})["reason"] == "no such batch"


def test_a_resumed_batch_can_be_stopped_again(batches, monkeypatch):
    import threading
    gate = threading.Event()

    def slow(record):
        gate.wait(5)
        return {"status": "cancelled", "error": "Stopped by the user before it finished."}
    rec = Recorder(slow)
    monkeypatch.setattr(batches.runner, "start_run", rec.start_run)
    monkeypatch.setattr(batches.runner, "get_run", rec.get_run)
    monkeypatch.setattr(batches.runner, "cancel_run", lambda run_id: gate.set())
    stopped_batch(batches, ["success", "pending", "pending", "pending"])

    batches.resume_batch("b1", {"id": "g1"})
    import time
    for _ in range(50):
        if rec.calls:
            break
        time.sleep(0.02)
    assert batches.cancel_batch("b1")["cancelled"] is True
    gate.set()
    assert batches.wait_for("b1", timeout=10)
    state = batches.get_batch("b1")
    assert state["status"] == "cancelled"
    assert state["items"][0]["status"] == "success"
    assert len(batches.unfinished(state)) == 3


def test_a_finished_step_after_a_rerun_step_runs_again_too(batches, monkeypatch):
    """Sleep Number, 8 Sep: 04-12 failed, 05-12 went on and succeeded without
    it. Resuming 04-12 alone would leave 05-12's verdict made from a memory
    that no longer exists; both run, in order."""
    rec = Recorder()
    monkeypatch.setattr(batches.runner, "start_run", rec.start_run)
    monkeypatch.setattr(batches.runner, "get_run", rec.get_run)
    stopped_batch(batches, ["success", "failed", "success", "cancelled"])

    outcome = batches.resume_batch("b1", {"id": "g1"})
    assert outcome["remaining"] == 3 and outcome["kept"] == 1 and outcome["rerun_after"] == 1
    assert batches.wait_for("b1", timeout=10)
    assert [a for _, a in rec.calls] == ["2026-02-01", "2026-03-01", "2026-04-01"]
    assert [i["status"] for i in batches.get_batch("b1")["items"]] == ["success"] * 4


def test_standalone_records_bring_nothing_along(batches, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(batches.runner, "start_run", rec.start_run)
    monkeypatch.setattr(batches.runner, "get_run", rec.get_run)
    state = stopped_batch(batches, ["success", "failed", "success"])
    for i in state["items"]:
        i["inputs"] = {"sample_id": f"s{i['index']}"}      # no as_of: not stepped
    batches._persist_batch(state)

    outcome = batches.resume_batch("b1", {"id": "g1"})
    assert outcome["remaining"] == 1 and outcome["rerun_after"] == 0
    assert batches.wait_for("b1", timeout=10)
    assert rec.calls == [("s1", None)]


class TestAFailedStepStopsItsSample:
    def test_later_steps_are_blocked_not_run(self, batches, monkeypatch):
        def outcome(record):
            if record["as_of"] == "2026-02-01":
                return {"status": "failed", "error": "boom", "node_error": {"code": "llm_failed"}}
            return {"status": "success", "result": {}}
        rec = Recorder(outcome)
        monkeypatch.setattr(batches.runner, "start_run", rec.start_run)
        monkeypatch.setattr(batches.runner, "get_run", rec.get_run)
        records = [{"sample_id": "acme", "as_of": f"2026-0{i}-01"} for i in (1, 2, 3, 4)]
        bid = batches.start_batch({"id": "g1"}, records, {"type": "credit_risk"}, workers=1)
        assert batches.wait_for(bid, timeout=10)

        state = batches.get_batch(bid)
        assert [a for _, a in rec.calls] == ["2026-01-01", "2026-02-01"]
        assert [i["status"] for i in state["items"]] == ["success", "failed", "blocked", "blocked"]
        assert "2026-02-01" in state["items"][2]["error"]
        assert state["status"] == "completed_with_errors"
        assert len(batches.unfinished(state)) == 3

    def test_an_unreachable_model_retries_the_step_and_then_the_blocked_ones(self, batches, monkeypatch):
        seen = []

        def outcome(record):
            seen.append(record["as_of"])
            if record["as_of"] == "2026-02-01" and seen.count("2026-02-01") == 1:
                return {"status": "failed", "error": "…",
                        "node_error": {"code": "llm_unreachable", "retryable": True}}
            return {"status": "success", "result": {}}
        rec = Recorder(outcome)
        monkeypatch.setattr(batches.runner, "start_run", rec.start_run)
        monkeypatch.setattr(batches.runner, "get_run", rec.get_run)
        records = [{"sample_id": "acme", "as_of": f"2026-0{i}-01"} for i in (1, 2, 3)]
        bid = batches.start_batch({"id": "g1"}, records, {"type": "credit_risk"}, workers=1)
        assert batches.wait_for(bid, timeout=10)

        assert [a for _, a in rec.calls] == ["2026-01-01", "2026-02-01", "2026-02-01", "2026-03-01"]
        assert [i["status"] for i in batches.get_batch(bid)["items"]] == ["success"] * 3
        assert batches.get_batch(bid)["status"] == "succeeded"
