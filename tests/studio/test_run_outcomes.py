"""Phase 4 of RUN_INPUT_FLOW_PLAN.md: outcomes and errors people can read.

A run record said `status: failed` and `error: <traceback>`, and a batch with
failed records said `completed` in green. These pin what a run remembers
about how it was made, what it says when it fails, and what a batch's
outcome is called. Written failing.
"""

import pytest

from conftest import make_graph, make_task


def chain():
    return make_graph(
        [make_task("a", inputs=["topic"], outputs=["x"]),
         make_task("b", inputs=["x"], outputs=["y"])],
        edges=[("a", "b")],
    )


class TestARunRemembersHowItWasMade:
    @pytest.fixture
    def client(self, studio_data, monkeypatch):
        from fastapi.testclient import TestClient
        from backend.api import app as studio_app

        launched = []
        monkeypatch.setattr(studio_app.runner, "start_run",
                            lambda graph, inputs, **kw: launched.append(kw) or "run-fake")
        monkeypatch.setattr(studio_app.runner, "list_runs", lambda graph_id=None: [])
        monkeypatch.setattr(studio_app.runner, "get_run", lambda run_id: None)
        tc = TestClient(studio_app.app)
        tc.launched = launched
        return tc

    def test_the_plan_and_revision_reach_the_run(self, client):
        from backend.api import graphs as graph_store
        created = graph_store.create_graph("G", "g")
        gid = graph_store.save_graph(created["id"], chain())["id"]
        plan = client.post(f"/api/graphs/{gid}/run-plan", json={}).json()
        res = client.post(f"/api/graphs/{gid}/run",
                          json={"plan_id": plan["plan_id"], "inputs": {"topic": "rates"}})
        assert res.status_code == 200, res.text
        assert client.launched[0]["plan_id"] == plan["plan_id"]
        assert client.launched[0]["graph_revision"] == plan["graph_revision"]


@pytest.fixture
def engine(studio_data, monkeypatch):
    """The runner with the model swapped for a recorder, as in the flow tests."""
    from evoagentx.models import LiteLLMConfig
    from backend.api import runner, tools_registry

    async def fake_llm(agent, task, inputs, state):
        if task["name"] == "boom":
            raise RuntimeError("model said no")
        if task["name"] == "offline":
            # As the framework delivers it: wrapped twice on the way up, the
            # cause still attached.
            from evoagentx.models.litellm_model import TransientLLMError
            try:
                raise TransientLLMError("Could not reach 'openai/x' for 15 minutes "
                                        "(InternalServerError: Connection error).",
                                        900.0, OSError("Connection error"))
            except TransientLLMError as e:
                raise RuntimeError(f"Error during single_generate_async of LiteLLM: {e}") from e
        return {o["name"]: f"{task['name']}:{sorted(inputs)}" for o in task.get("outputs") or []}

    class StubLLM:
        config = LiteLLMConfig(model="deepseek/deepseek-chat", deepseek_key="test-only")

    monkeypatch.setattr(runner, "execute_llm_node", fake_llm)
    monkeypatch.setattr(runner, "_make_llm", lambda **kw: StubLLM())
    monkeypatch.setattr(runner, "_prepare_ltm", lambda doc, ordered, inputs, state: ({}, ordered))
    monkeypatch.setattr(runner, "_attach_ltm", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_save_ltm", lambda *a, **k: None)
    monkeypatch.setattr(tools_registry, "find_tool", lambda n: ("kit", n) if n == "broken" else None)
    monkeypatch.setattr(tools_registry, "validate_tool_names", lambda names: None)
    monkeypatch.setattr(tools_registry, "resolve_tools", lambda names, **kw: None)
    monkeypatch.setattr(tools_registry, "call_tool",
                        lambda name, args, **kw: (_ for _ in ()).throw(ValueError("bad arg")))

    def run(g, inputs=None, **kw):
        g = {**g, "id": "outcome-e2e", "flow_version": 2}
        return runner.get_run(runner.start_run(g, inputs or {}, background=False, **kw))
    return run


class TestAFailureIsReadableFirst:
    def test_a_missing_input_names_the_node_and_field_not_a_traceback(self, engine):
        g = make_graph([make_task("a", inputs=["topic"], outputs=["x"])])
        g["edges"] = []
        run = engine(g, {})

        assert run["status"] == "failed"
        assert "Traceback" not in run["error"]
        assert "topic" in run["error"] and "'a'" in run["error"]
        assert run["node_error"] == {"code": "missing_input", "node": "a", "field": "topic",
                                     "message": run["error"]}
        assert "Traceback" in run["debug_error"]

    def test_a_model_failure_is_pinned_to_its_node(self, engine):
        g = make_graph([make_task("boom", inputs=["topic"], outputs=["x"])]); g["edges"] = []
        run = engine(g, {"topic": "t"})

        assert run["status"] == "failed"
        assert run["node_error"]["code"] == "llm_failed"
        assert run["node_error"]["node"] == "boom"
        assert "model said no" in run["error"]
        assert "Traceback" not in run["error"]

    def test_a_model_that_could_not_be_reached_is_a_retryable_failure(self, engine):
        g = make_graph([make_task("offline", inputs=["topic"], outputs=["x"])]); g["edges"] = []
        run = engine(g, {"topic": "t"})

        assert run["status"] == "failed"
        assert run["node_error"]["code"] == "llm_unreachable"
        assert run["node_error"]["retryable"] is True
        assert "could not reach the model" in run["error"]
        assert "15 minutes" in run["error"]

    def test_a_tool_failure_is_pinned_to_its_node(self, engine):
        t = make_task("fix", inputs=["topic"], outputs=["y"]); t["kind"] = "tool"; t["tool"] = "broken"
        g = make_graph([t]); g["edges"] = []
        run = engine(g, {"topic": "t"})

        assert run["node_error"]["code"] == "tool_failed"
        assert run["node_error"]["node"] == "fix"
        assert "bad arg" in run["error"]

    def test_the_run_keeps_what_it_was_started_with(self, engine):
        g = make_graph([make_task("a", inputs=["topic"], outputs=["x"])]); g["edges"] = []
        run = engine(g, {"topic": "rates " * 40}, plan_id="plan:1", graph_revision="rev:1")

        assert run["plan_id"] == "plan:1"
        assert run["graph_revision"] == "rev:1"
        # A summary, not the payload: enough to recognise the run in a list.
        assert run["input_summary"] == {"topic": ("rates " * 40)[:80] + "…"}


class TestABatchSaysHowItEnded:
    @pytest.fixture
    def batches(self, tmp_path, monkeypatch):
        from backend.api import batch as batch_module
        monkeypatch.setattr(batch_module, "BATCHES_DIR", tmp_path / "batches")
        batch_module._batches.clear(); batch_module._threads.clear()
        return batch_module

    def finish(self, batches, monkeypatch, outcomes):
        from backend.api import runner
        seq = iter(outcomes)
        def fake_start(graph, record, **kw):
            status = next(seq)
            run_id = kw.get("run_id") or "r"
            runner._runs[run_id] = {"run_id": run_id, "status": status, "result": {"x": 1},
                                    "error": None if status == "success" else "boom"}
            return run_id
        monkeypatch.setattr(runner, "start_run", fake_start)
        bid = batches.start_batch({"id": "g1", "tasks": [], "edges": []},
                                  [{"i": n} for n in range(len(outcomes))], {"type": "upload"},
                                  workers=1)
        batches.wait_for(bid)
        return batches.get_batch(bid)

    def test_all_records_succeeding_is_succeeded(self, batches, monkeypatch):
        assert self.finish(batches, monkeypatch, ["success", "success"])["status"] == "succeeded"

    def test_any_failed_record_is_completed_with_errors_not_green(self, batches, monkeypatch):
        b = self.finish(batches, monkeypatch, ["success", "failed", "success"])
        assert b["status"] == "completed_with_errors"

    def test_a_batch_that_never_ran_a_record_is_failed(self, batches, monkeypatch):
        from backend.api import runner
        def blow(graph, record, **kw): raise RuntimeError("cannot even start")
        monkeypatch.setattr(runner, "start_run", blow)
        bid = batches.start_batch({"id": "g1", "tasks": [], "edges": []},
                                  [{"i": 0}], {"type": "upload"}, workers=1)
        batches.wait_for(bid)
        assert batches.get_batch(bid)["status"] == "failed"
