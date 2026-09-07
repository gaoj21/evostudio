"""What happens to work in flight when the server restarts.

Runs and batches execute in threads; a restart kills them but leaves their
records claiming to be running. Execution cannot be resumed — repeating LLM
calls would bill the user twice for a result they may already have — so the
contract is narrower and worth pinning: a run must be *persisted the moment it
starts*, and a leftover "running" record must be turned into a stated failure
at startup.

This is the bug the user actually hit: the server restarted, a generation was
lost, the UI polled its id forever, and asking again started a second one.
"""

import json

import pytest

from conftest import make_graph, make_task


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    import batch as batch_store
    import runner

    runs = tmp_path / "runs"
    batches = tmp_path / "batches"
    runs.mkdir()
    batches.mkdir()
    monkeypatch.setattr(runner, "RUNS_DIR", runs)
    monkeypatch.setattr(batch_store, "BATCHES_DIR", batches)
    return runs, batches


def write(path, **fields):
    path.write_text(json.dumps(fields, indent=2), encoding="utf-8")


class TestRunsSurviveTheProcess:
    def test_a_run_is_on_disk_before_it_finishes(self, dirs, studio_data, monkeypatch):
        """Persisting only on completion is what made a lost run 404."""
        import runner

        # Never actually execute: the point is what exists the moment it starts.
        monkeypatch.setattr(runner, "_execute_run", lambda *a, **k: None)
        graph = make_graph([make_task("a", inputs=["topic"], outputs=["x"])],
                           id="g1")
        run_id = runner.start_run(graph, {"topic": "t"}, background=False)

        runs, _ = dirs
        stored = json.loads((runs / f"{run_id}.json").read_text())
        assert stored["run_id"] == run_id
        assert stored["graph_id"] == "g1"
        assert stored["inputs"] == {"topic": "t"}

    def test_a_leftover_running_record_becomes_a_stated_failure(self, dirs):
        import runner

        runs, _ = dirs
        write(runs / "ghost.json", run_id="ghost", status="running", error=None)

        assert runner.mark_interrupted() == 1
        stored = json.loads((runs / "ghost.json").read_text())
        assert stored["status"] == "failed"
        assert "restart" in stored["error"]

    def test_finished_runs_are_left_alone(self, dirs):
        import runner

        runs, _ = dirs
        write(runs / "ok.json", run_id="ok", status="success", result={"a": 1})
        write(runs / "bad.json", run_id="bad", status="failed", error="a real error")

        runner.mark_interrupted()
        assert json.loads((runs / "ok.json").read_text())["result"] == {"a": 1}
        assert json.loads((runs / "bad.json").read_text())["error"] == "a real error"

    def test_unreadable_files_do_not_stop_the_sweep(self, dirs):
        import runner

        runs, _ = dirs
        (runs / "broken.json").write_text("{not json", encoding="utf-8")
        write(runs / "ghost.json", run_id="ghost", status="running")

        assert runner.mark_interrupted() == 1

    def test_the_sweep_is_idempotent(self, dirs):
        import runner

        runs, _ = dirs
        write(runs / "ghost.json", run_id="ghost", status="running")
        assert runner.mark_interrupted() == 1
        assert runner.mark_interrupted() == 0


class TestBatchesSurviveTheProcess:
    def test_a_leftover_batch_keeps_what_it_finished(self, dirs):
        """An interrupted batch is still worth reading: the items that
        completed have real results, and scores worth keeping."""
        import batch as batch_store

        _, batches = dirs
        write(batches / "b1.json", batch_id="b1", status="running", items=[
            {"index": 0, "status": "success", "score": 1.0},
            {"index": 1, "status": "running"},
            {"index": 2, "status": "pending"},
        ])

        assert batch_store.mark_interrupted() == 1
        stored = json.loads((batches / "b1.json").read_text())
        assert stored["status"] == "interrupted"
        assert stored["items"][0] == {"index": 0, "status": "success", "score": 1.0}
        assert stored["items"][1]["status"] == "failed"
        assert "restart" in stored["items"][1]["error"].lower()
        assert stored["items"][2]["status"] == "failed"

    def test_a_completed_batch_is_untouched(self, dirs):
        import batch as batch_store

        _, batches = dirs
        write(batches / "done.json", batch_id="done", status="completed",
              items=[{"index": 0, "status": "success"}], summary={"mean": 1.0})

        assert batch_store.mark_interrupted() == 0
        assert json.loads((batches / "done.json").read_text())["summary"] == {"mean": 1.0}
