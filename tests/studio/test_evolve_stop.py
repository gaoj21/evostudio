"""Stopping an Evolve task, and telling a cut-off one from a running one.

POST /api/evolve/{id}/stop stops a running task at its next record and
cancels the run in flight; the task ends "stopped" with nothing applied. A
task persisted as running that this process is not running was cut off by a
restart and reads as "interrupted".
"""

import json
import time

import pytest

evolve_api = pytest.importorskip("backend.api.evolve_api")


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from backend.api import app
    monkeypatch.setattr(evolve_api, "EVOLVE_DIR", tmp_path / "evolve")
    monkeypatch.setattr(evolve_api, "_tasks", {})
    return TestClient(app.app)


def settle(task_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = evolve_api.get_task(task_id)
        if task and task["status"] != "running":
            return task
        time.sleep(0.02)
    pytest.fail("Evolve task did not finish")


GRAPH = {"id": "g-evolve", "tasks": [{"name": "a", "prompt": "Answer {q}"}], "edges": []}
PARAMS = {"mode": "evaluate", "evaluator": "ev", "nodes": [], "rounds": 1,
          "source": {"type": "canvas", "node": "feed", "config": {}}}
ROWS = [{"q": f"question {i}"} for i in range(4)]


class TestStoppingACanvasEvaluation:
    def test_stop_between_records_ends_it_stopped(self, client, monkeypatch):
        from backend.api import runner
        calls, cancelled, holder = [], [], {}

        def start_run(graph, record, background, run_id, **kw):
            calls.append(record["q"])
            # The user presses Stop while the replay is running.
            if "stop" not in holder:
                [task_id] = evolve_api._tasks
                res = client.post(f"/api/evolve/{task_id}/stop")
                holder["stop"] = (res.status_code, res.json())
            return run_id

        monkeypatch.setattr(runner, "start_run", start_run)
        monkeypatch.setattr(runner, "get_run", lambda run_id: {"status": "cancelled"})
        monkeypatch.setattr(runner, "cancel_run", lambda run_id, **kw: None)
        from backend.api import batch
        real_cancel = batch.cancel_batch
        monkeypatch.setattr(batch, "cancel_batch", lambda bid: cancelled.append(bid) or real_cancel(bid))
        from backend.features.evaluation import canvas_evolution
        scored = []                     # a stopped task is never scored
        monkeypatch.setattr(canvas_evolution, "score_saved", lambda *a: scored.append(a))

        holder["id"] = evolve_api.start_evolve(GRAPH, ROWS, "canvas:ev", PARAMS)

        task = settle(holder["id"])
        assert holder["stop"] == (200, {"stopping": True})
        assert task["status"] == "stopped" and task["error"] is None
        # The replay is a batch: Stop cancels it, and no later candidate starts.
        assert cancelled and len(set(cancelled)) == 1
        assert len(calls) <= len(ROWS)
        assert scored == []
        assert task["baseline"] is None and not task.get("optimized_graph")
        persisted = json.loads((evolve_api._task_dir(holder["id"]) / "result.json").read_text())
        assert persisted["status"] == "stopped"

    def test_a_finished_task_cannot_be_stopped(self, client, monkeypatch):
        from backend.api import runner
        from backend.features.evaluation import canvas_evolution
        monkeypatch.setattr(runner, "start_run", lambda graph, record, background, run_id, **kw: run_id)
        monkeypatch.setattr(runner, "get_run", lambda run_id: {"status": "success", "result": "ok"})
        monkeypatch.setattr(canvas_evolution, "score_saved",
                            lambda *a: {"metrics": {"score": 1.0}, "records": {}})
        task_id = evolve_api.start_evolve(GRAPH, ROWS, "canvas:ev", PARAMS)
        assert settle(task_id)["status"] == "done"
        res = client.post(f"/api/evolve/{task_id}/stop")
        assert res.status_code == 409

    def test_an_unknown_task_is_a_404(self, client):
        assert client.post("/api/evolve/nope/stop").status_code == 404


class TestATaskCutOffByARestart:
    def persisted_running(self, task_id="old1"):
        folder = evolve_api._task_dir(task_id)
        folder.mkdir(parents=True)
        (folder / "result.json").write_text(json.dumps({
            "task_id": task_id, "graph_id": "g-evolve", "status": "running",
            "stage": "baseline: record 3/10", "created_at": "2026-09-01T00:00:00+00:00"}))
        return task_id

    def test_it_reads_as_interrupted(self, client):
        task_id = self.persisted_running()
        task = client.get(f"/api/evolve/{task_id}").json()
        assert task["status"] == "interrupted" and task["stage"] is None
        assert "restarted" in task["error"]
        listed = client.get("/api/evolve", params={"graph_id": "g-evolve"}).json()
        assert [t["status"] for t in listed if t["task_id"] == task_id] == ["interrupted"]

    def test_it_cannot_be_stopped(self, client):
        task_id = self.persisted_running()
        assert client.post(f"/api/evolve/{task_id}/stop").status_code == 409


def test_metrics_are_the_platform_s_own(client):
    names = {m["name"] for m in client.get("/api/evolve/metrics").json()["metrics"]}
    assert {"exact_match", "contains", "numeric"} <= names
    assert "credit_risk" not in names


def test_a_model_call_blocked_in_the_provider_does_not_hold_the_stop(client, monkeypatch):
    """Proposing prompts is one long model call. A provider SDK that blocks
    for minutes used to keep the task running until it answered; Stop now
    gives the call up and the task ends stopped, at once."""
    import threading
    from types import SimpleNamespace
    from backend.features import model_bridge
    from backend.features.evaluation import canvas_evolution
    entered, release = threading.Event(), threading.Event()

    def chat_result(provider, messages, **options):
        entered.set()
        release.wait(30)                     # the provider takes its time
        return SimpleNamespace(content='late', usage=None, provider=provider, model='m')
    monkeypatch.setattr(model_bridge, 'chat_result', chat_result)
    monkeypatch.setattr(canvas_evolution, 'execute',
                        lambda state, graph, rows, params, stage: model_bridge._call('p', [], {}))
    task_id = evolve_api.start_evolve(GRAPH, ROWS, "canvas:ev", PARAMS)
    try:
        assert entered.wait(5)
        started = time.time()
        assert client.post(f"/api/evolve/{task_id}/stop").status_code == 200
        task = settle(task_id, timeout=5)
        assert task["status"] == "stopped" and task["error"] is None
        assert time.time() - started < 3
    finally:
        release.set()
