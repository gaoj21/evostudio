"""Stopping work that is already under way.

A batch is the expensive case: hundreds of records, each several LLM calls.
Neither engine can interrupt a `WorkFlow.execute()` already in flight, so what
is tested here is the honest version of stopping — a batch starts no further
items, and a run stops being reported on while its thread finishes unattended.
"""

import threading
import time

import pytest


@pytest.fixture
def batch_store(tmp_path, monkeypatch):
    import batch as batch_module

    monkeypatch.setattr(batch_module, "BATCHES_DIR", tmp_path / "batches")
    batch_module._batches.clear()
    batch_module._threads.clear()
    yield batch_module

    # A batch writes itself from its worker thread *after* setting its final
    # status, so a test that only waits for the status can end while that write
    # is still pending — and it would then land in the developer's real
    # studio/data once monkeypatch has restored the path.
    for batch_id in list(batch_module._threads):
        assert batch_module.wait_for(batch_id, timeout=10), \
            f"batch {batch_id} did not wind down; its state would leak"


@pytest.fixture
def slow_runner(monkeypatch, batch_store):
    """Replaces the run engine with one that blocks until released.

    Records which items actually started, which is the whole question: a
    cancelled batch must leave the rest untouched.
    """
    import runner

    started: list[dict] = []
    # One permit per item allowed through, so a test can let exactly as many
    # items run as it means to; without that the batch races to completion
    # before it can be cancelled.
    permits = threading.Semaphore(0)

    def fake_start_run(graph, inputs, background=True, gray_zone=None,
                       run_id=None, start_at=None):
        started.append(inputs)
        assert permits.acquire(timeout=5)
        runner._runs[run_id] = {
            "run_id": run_id, "status": "success", "result": {"echo": inputs},
            "nodes": [], "error": None, "review_status": None,
        }
        return run_id

    monkeypatch.setattr(batch_store.runner, "start_run", fake_start_run)
    monkeypatch.setattr(batch_store.runner, "get_run",
                        lambda rid: runner._runs.get(rid))

    def allow(count=1):
        for _ in range(count):
            permits.release()

    return started, allow


def _wait_until(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _await_status(batch_store, batch_id, status, timeout=5.0):
    assert _wait_until(
        lambda: batch_store.get_batch(batch_id)["status"] == status, timeout
    ), f"batch never reached {status!r}"


class TestCancelBatch:
    def test_cancelling_leaves_the_unstarted_records_alone(self, batch_store, slow_runner):
        started, allow = slow_runner
        records = [{"n": i} for i in range(20)]
        batch_id = batch_store.start_batch({"id": "g"}, records, {"type": "manual"}, workers=2)

        # Two workers, so two items are in flight and the rest are untouched.
        assert _wait_until(lambda: len(started) == 2)
        outcome = batch_store.cancel_batch(batch_id)
        assert outcome["cancelled"] is True
        assert outcome["still_running"] == 2
        assert outcome["not_started"] == 18

        allow(2)
        _await_status(batch_store, batch_id, "cancelled")

        # The 18 that had not begun never called the run engine at all: that is
        # the LLM spend the cancel actually saves.
        assert len(started) == 2

    def test_the_work_that_did_run_is_kept(self, batch_store, slow_runner):
        started, allow = slow_runner
        batch_id = batch_store.start_batch(
            {"id": "g"}, [{"n": i} for i in range(6)], {"type": "manual"}, workers=1
        )
        assert _wait_until(lambda: len(started) == 1)
        allow(1)   # let the first item finish, so there is a result worth keeping
        assert _wait_until(
            lambda: batch_store.get_batch(batch_id)["items"][0]["status"] == "success"
        )
        batch_store.cancel_batch(batch_id)
        allow(5)   # release whatever the worker had already picked up
        _await_status(batch_store, batch_id, "cancelled")

        items = batch_store.get_batch(batch_id)["items"]
        assert items[0]["status"] == "success"
        assert items[0]["output_summary"]
        assert [i["status"] for i in items[-2:]] == ["cancelled", "cancelled"]

    def test_it_says_it_is_cancelling_before_the_last_item_lets_go(
        self, batch_store, slow_runner
    ):
        started, allow = slow_runner
        batch_id = batch_store.start_batch(
            {"id": "g"}, [{"n": i} for i in range(8)], {"type": "manual"}, workers=2
        )
        assert _wait_until(lambda: len(started) == 2)
        batch_store.cancel_batch(batch_id)

        # Still winding down: claiming "cancelled" here would be a lie while
        # two runs are still burning tokens.
        assert batch_store.get_batch(batch_id)["status"] == "cancelling"
        allow(2)
        _await_status(batch_store, batch_id, "cancelled")

    def test_a_cancelled_evaluation_still_scores_what_it_covered(
        self, batch_store, slow_runner, monkeypatch
    ):
        import evaluation

        monkeypatch.setattr(evaluation, "score_one",
                            lambda metric, pred, label: {"score": 1.0})
        started, allow = slow_runner
        batch_id = batch_store.start_batch(
            {"id": "g"}, [{"n": i} for i in range(10)], {"type": "manual"},
            workers=1, metric="exact_match", labels=["x"] * 10,
        )
        assert _wait_until(lambda: len(started) == 1)
        allow(1)
        assert _wait_until(
            lambda: batch_store.get_batch(batch_id)["items"][0]["score"] == 1.0
        )
        batch_store.cancel_batch(batch_id)
        allow(5)
        _await_status(batch_store, batch_id, "cancelled")

        summary = batch_store.get_batch(batch_id)["summary"]
        # A partial mean is useful, but only alongside how little it covers.
        assert summary["mean"] == 1.0
        assert summary["scored"] < 10

    def test_the_cancellation_survives_a_restart(self, batch_store, slow_runner):
        started, allow = slow_runner
        batch_id = batch_store.start_batch(
            {"id": "g"}, [{"n": i} for i in range(6)], {"type": "manual"}, workers=1
        )
        assert _wait_until(lambda: len(started) == 1)
        batch_store.cancel_batch(batch_id)
        allow(2)
        _await_status(batch_store, batch_id, "cancelled")

        batch_store._batches.clear()   # as if the process had restarted
        assert batch_store.get_batch(batch_id)["status"] == "cancelled"

    def test_cancelling_twice_is_not_an_error(self, batch_store, slow_runner):
        started, allow = slow_runner
        batch_id = batch_store.start_batch(
            {"id": "g"}, [{"n": i} for i in range(6)], {"type": "manual"}, workers=1
        )
        assert _wait_until(lambda: len(started) == 1)
        assert batch_store.cancel_batch(batch_id)["cancelled"] is True
        assert batch_store.cancel_batch(batch_id)["already_requested"] is True
        allow(2)
        _await_status(batch_store, batch_id, "cancelled")

    def test_a_finished_batch_cannot_be_cancelled(self, batch_store, slow_runner):
        started, allow = slow_runner
        allow(2)
        batch_id = batch_store.start_batch(
            {"id": "g"}, [{"n": 1}], {"type": "manual"}, workers=1
        )
        _await_status(batch_store, batch_id, "completed")
        outcome = batch_store.cancel_batch(batch_id)
        assert outcome["cancelled"] is False
        assert "completed" in outcome["reason"]

    def test_an_unknown_batch_is_reported_as_such(self, batch_store):
        assert batch_store.cancel_batch("nope") == {
            "cancelled": False, "reason": "no such batch"
        }


class TestAbandonRun:
    @pytest.fixture
    def runs(self, tmp_path, monkeypatch):
        import runner

        monkeypatch.setattr(runner, "RUNS_DIR", tmp_path / "runs")
        runner._runs.clear()
        return runner

    def test_an_abandoned_run_stops_claiming_to_be_running(self, runs):
        runs._runs["r1"] = {"run_id": "r1", "status": "running", "nodes": []}
        outcome = runs.abandon_run("r1")

        assert outcome == {"abandoned": True, "still_executing": True}
        run = runs.get_run("r1")
        assert run["status"] == "abandoned"
        # The user is told plainly that this does not save them the tokens.
        assert "cannot be interrupted" in run["error"]

    def test_a_late_success_does_not_overwrite_what_the_user_was_told(self, runs):
        state = {"run_id": "r1", "status": "running", "nodes": []}
        runs._runs["r1"] = state
        runs.abandon_run("r1")

        # The thread the user walked away from finishes minutes later.
        runs._set_status(state, "success", result={"answer": 42})

        run = runs.get_run("r1")
        assert run["status"] == "abandoned"
        assert run["finished_after_abandon"] == "success"
        assert run["late_result"] == {"answer": 42}

    def test_a_late_failure_does_not_replace_the_abandonment_message(self, runs):
        state = {"run_id": "r1", "status": "running", "nodes": []}
        runs._runs["r1"] = state
        runs.abandon_run("r1")
        runs._set_status(state, "failed", error="Traceback: boom")

        run = runs.get_run("r1")
        assert run["status"] == "abandoned"
        assert "cannot be interrupted" in run["error"]
        assert run["late_error"] == "Traceback: boom"

    def test_abandoning_is_persisted_immediately(self, runs):
        runs._runs["r1"] = {"run_id": "r1", "status": "running", "nodes": []}
        runs.abandon_run("r1")

        runs._runs.clear()   # as if the process had restarted
        assert runs.get_run("r1")["status"] == "abandoned"

    def test_a_finished_run_cannot_be_abandoned(self, runs):
        runs._runs["r1"] = {"run_id": "r1", "status": "success", "nodes": []}
        outcome = runs.abandon_run("r1")
        assert outcome == {"abandoned": False, "reason": "run already success"}

    def test_a_run_stranded_by_a_restart_is_tidied_up(self, runs):
        runs._persist_run({"run_id": "old", "status": "running", "nodes": []})
        outcome = runs.abandon_run("old")

        # Nothing is executing, so this is housekeeping, not an abandonment.
        assert outcome == {"abandoned": True, "still_executing": False}
        assert runs.get_run("old")["status"] == "abandoned"

    def test_an_unknown_run_is_reported_as_such(self, runs):
        assert runs.abandon_run("nope") == {"abandoned": False, "reason": "no such run"}


class TestEndpoints:
    """The HTTP wiring: the UI reaches both of these through one POST each."""

    @pytest.fixture
    def client(self, batch_store, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        import app as studio_app
        import runner

        monkeypatch.setattr(runner, "RUNS_DIR", tmp_path / "runs")
        runner._runs.clear()
        return TestClient(studio_app.app)

    def test_cancelling_a_live_batch(self, client, batch_store, slow_runner):
        started, allow = slow_runner
        batch_id = batch_store.start_batch(
            {"id": "g"}, [{"n": i} for i in range(12)], {"type": "manual"}, workers=2
        )
        assert _wait_until(lambda: len(started) == 2)

        res = client.post(f"/api/batches/{batch_id}/cancel")
        assert res.status_code == 200
        body = res.json()
        assert body["cancelled"] is True
        assert body["not_started"] == 10
        assert body["still_running"] == 2

        allow(2)
        _await_status(batch_store, batch_id, "cancelled")
        assert client.get(f"/api/batches/{batch_id}").json()["status"] == "cancelled"
        assert len(started) == 2

    def test_cancelling_an_unknown_batch_is_a_404(self, client):
        assert client.post("/api/batches/nope/cancel").status_code == 404

    def test_cancelling_a_finished_batch_is_not_a_404(self, client, batch_store, slow_runner):
        # It exists, so 404 would be wrong; the body says why nothing happened.
        started, allow = slow_runner
        allow(2)
        batch_id = batch_store.start_batch(
            {"id": "g"}, [{"n": 1}], {"type": "manual"}, workers=1
        )
        _await_status(batch_store, batch_id, "completed")
        res = client.post(f"/api/batches/{batch_id}/cancel")
        assert res.status_code == 200
        assert res.json()["cancelled"] is False

    def test_abandoning_a_run(self, client, batch_store):
        import runner

        runner._runs["r1"] = {"run_id": "r1", "status": "running", "nodes": []}
        res = client.post("/api/runs/r1/abandon")
        assert res.status_code == 200
        assert res.json() == {"abandoned": True, "still_executing": True}
        assert client.get("/api/runs/r1").json()["status"] == "abandoned"

    def test_abandoning_an_unknown_run_is_a_404(self, client):
        assert client.post("/api/runs/nope/abandon").status_code == 404
