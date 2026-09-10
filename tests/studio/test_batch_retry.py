"""A record the model could not be reached for is run again, later.

An outage that outlasts one run's waiting window rarely outlasts the batch:
the record goes back in the queue once the others have had their turn, after
a pause. A record that failed for any other reason stays failed — running a
malformed prompt again produces the same error.
"""

import pytest


@pytest.fixture
def batches(tmp_path, monkeypatch):
    from backend.api import batch as batch_module
    monkeypatch.setattr(batch_module, "BATCHES_DIR", tmp_path / "batches")
    monkeypatch.setattr(batch_module, "_pause", lambda seconds: None)
    batch_module._batches.clear()
    batch_module._threads.clear()
    return batch_module


class FakeRunner:
    """start_run/get_run with a script: outcomes per record, per attempt."""

    def __init__(self, script):
        self.script = script          # {sample_id: [run_state, run_state, ...]}
        self.runs = {}
        self.calls = []

    def start_run(self, graph, record, background, gray_zone, run_id, batch_id,
                  session_started_at):
        sid = record["sample_id"]
        self.calls.append(sid)
        outcomes = self.script[sid]
        state = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        self.runs[run_id] = dict(state)

    def get_run(self, run_id):
        return self.runs.get(run_id)


UNREACHABLE = {"status": "failed", "error": "Node 'decide' could not reach the model: …",
               "node_error": {"code": "llm_unreachable", "node": "decide", "retryable": True}}
BROKEN = {"status": "failed", "error": "Node 'decide' needs 'context'",
          "node_error": {"code": "missing_input", "node": "decide"}}
OK = {"status": "success", "result": {"decision": "suppress"}}


def run_batch(batches, monkeypatch, script):
    fake = FakeRunner(script)
    monkeypatch.setattr(batches.runner, "start_run", fake.start_run)
    monkeypatch.setattr(batches.runner, "get_run", fake.get_run)
    records = [{"sample_id": sid} for sid in script]
    bid = batches.start_batch({"id": "g1"}, records, {"type": "upload"}, workers=1)
    assert batches.wait_for(bid, timeout=10)
    return fake, batches._batches[bid]


def test_an_unreachable_model_gets_the_record_a_second_turn(batches, monkeypatch):
    fake, state = run_batch(batches, monkeypatch, {"a": [UNREACHABLE, OK], "b": [OK]})
    assert fake.calls == ["a", "b", "a"]
    assert [i["status"] for i in state["items"]] == ["success", "success"]
    assert state["items"][0]["attempts"] == 2
    assert state["status"] == "succeeded"


def test_a_broken_record_is_not_run_again(batches, monkeypatch):
    fake, state = run_batch(batches, monkeypatch, {"a": [BROKEN], "b": [OK]})
    assert fake.calls == ["a", "b"]
    assert state["items"][0]["status"] == "failed"
    assert state["status"] == "completed_with_errors"


def test_after_the_passes_it_stays_failed_and_says_why(batches, monkeypatch):
    fake, state = run_batch(batches, monkeypatch, {"a": [UNREACHABLE]})
    assert fake.calls == ["a"] * (1 + batches.RETRY_PASSES)
    assert state["items"][0]["status"] == "failed"
    assert "could not reach the model" in state["items"][0]["error"]
    assert "retry_note" not in state


def test_a_cancelled_batch_does_not_retry(batches, monkeypatch):
    fake = FakeRunner({"a": [UNREACHABLE]})

    def start_and_cancel(graph, record, **kw):
        fake.start_run(graph, record, **kw)
        batches._batches[kw["batch_id"]]["cancel_requested"] = True
    monkeypatch.setattr(batches.runner, "start_run", start_and_cancel)
    monkeypatch.setattr(batches.runner, "get_run", fake.get_run)
    bid = batches.start_batch({"id": "g1"}, [{"sample_id": "a"}], {"type": "upload"}, workers=1)
    assert batches.wait_for(bid, timeout=10)
    assert fake.calls == ["a"]
