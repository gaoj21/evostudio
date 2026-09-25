"""Starting an optimization without reasoning about MIPRO's numbers."""

import pytest

from backend.api import evolve_api


class TestPresets:
    def test_a_preset_fills_candidates_and_steps(self):
        p = evolve_api.resolve_params({"preset": "standard"}, 10, ["a", "b"])
        assert (p["num_candidates"], p["max_steps"]) == (4, 4)

    def test_quick_is_the_default(self):
        assert evolve_api.resolve_params({}, 10, ["a"])["preset"] == "quick"

    def test_the_records_split_70_30_unless_told(self):
        p = evolve_api.resolve_params({}, 10, ["a"])
        assert (p["n_train"], p["n_dev"]) == (7, 3)
        p = evolve_api.resolve_params({"n_dev": 2}, 10, ["a"])
        assert (p["n_train"], p["n_dev"]) == (8, 2)

    def test_two_records_still_give_one_of_each(self):
        p = evolve_api.resolve_params({}, 2, ["a"])
        assert (p["n_train"], p["n_dev"]) == (1, 1)

    def test_an_explicit_number_beats_the_preset(self):
        p = evolve_api.resolve_params({"preset": "quick", "max_steps": "6"}, 10, ["a"])
        assert p["max_steps"] == 6 and p["num_candidates"] == 2

    def test_nodes_default_to_every_llm_node_and_ignore_unknown_names(self):
        assert evolve_api.resolve_params({}, 4, ["detect", "decide"])["nodes"] == ["detect", "decide"]
        p = evolve_api.resolve_params({"nodes": "decide, nope"}, 4, ["detect", "decide"])
        assert p["nodes"] == ["decide"]
        p = evolve_api.resolve_params({"nodes": ["detect"]}, 4, ["detect", "decide"])
        assert p["nodes"] == ["detect"]

    def test_an_unknown_preset_is_refused_by_name(self):
        from backend.api import sources
        with pytest.raises(sources.SourceError, match="Unknown preset 'huge'"):
            evolve_api.resolve_params({"preset": "huge"}, 4, ["a"])


class TestApplyInPlace:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        from backend.api import app as studio_app, graphs as graph_store
        monkeypatch.setattr(graph_store, "GRAPHS_DIR", tmp_path / "graphs")
        monkeypatch.setattr(evolve_api, "EVOLVE_DIR", tmp_path / "evolve")
        g = graph_store.create_graph("Credit Risk", "goal")
        graph_store.save_graph(g["id"], {**g, "tasks": [
            {"name": "detect", "prompt": "old detect", "inputs": [], "outputs": []},
            {"name": "decide", "prompt": "old decide", "inputs": [], "outputs": []}], "edges": []})
        evolve_api._tasks.clear()
        evolve_api._tasks["t1"] = {
            "task_id": "t1", "graph_id": g["id"], "status": "done",
            "optimized_graph": {**g, "tasks": [
                {"name": "detect", "prompt": "old detect"},
                {"name": "decide", "prompt": "NEW decide"}]},
        }
        return TestClient(studio_app.app), graph_store, g["id"]

    def test_replace_writes_the_prompts_into_the_same_workflow_and_keeps_a_copy(self, client):
        c, store, gid = client
        res = c.post(f"/api/evolve/t1/apply?mode=replace")
        assert res.status_code == 200, res.text
        now = store.load_graph(gid)
        assert {t["name"]: t["prompt"] for t in now["tasks"]} == {"detect": "old detect", "decide": "NEW decide"}
        backup = store.load_graph(res.json()["backup_id"])
        assert backup["name"] == "Credit Risk (before evolve)"
        assert {t["name"]: t["prompt"] for t in backup["tasks"]}["decide"] == "old decide"

    def test_new_still_saves_beside_the_original(self, client):
        c, store, gid = client
        res = c.post("/api/evolve/t1/apply")
        assert res.status_code == 200
        assert res.json()["id"] != gid
        assert {t["name"]: t["prompt"] for t in store.load_graph(gid)["tasks"]}["decide"] == "old decide"

    def test_a_bad_mode_is_refused(self, client):
        c, _, _ = client
        assert c.post("/api/evolve/t1/apply?mode=sideways").status_code == 422

    def test_presets_are_listed_for_the_form(self, client):
        c, _, _ = client
        names = [p["name"] for p in c.get("/api/evolve/presets").json()["presets"]]
        assert names == ["quick", "standard", "thorough"]


def test_evaluation_only_uses_every_record_without_training():
    p = evolve_api.resolve_params({"mode": "evaluate", "n_train": 99}, 3, ["a"])
    assert p["n_train"] == 0 and p["n_dev"] == 3
    assert p["nodes"] == []


def test_evaluation_only_executes_without_optimizer(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from backend.api import runner, tools_registry
    from evoagentx.agents.agent_manager import AgentManager
    from evoagentx.evaluators import Evaluator
    from evoagentx.optimizers.mipro_optimizer import WorkFlowMiproOptimizer
    records = [{"id": str(i), "inputs": {"text": str(i)}, "label": str(i)} for i in range(3)]
    (tmp_path / "dataset.jsonl").write_text("\n".join(__import__('json').dumps(r) for r in records))
    monkeypatch.setattr(runner, "_make_llm", lambda **kw: SimpleNamespace(config=None))
    monkeypatch.setattr(AgentManager, "add_agents_from_workflow", lambda *a, **kw: None)
    monkeypatch.setattr(tools_registry, "resolve_tools", lambda *a, **kw: [])
    monkeypatch.setattr(Evaluator, "__init__", lambda *a, **kw: None)
    def evaluate(self, graph, benchmark, **kw):
        assert benchmark._train_data == []
        assert len(benchmark._dev_data) == 3
        assert graph.nodes[0].agents[0]["prompt"] == "Original prompt"
        self._evaluation_records = {r["id"]: {"prediction": r["label"], "label": r["label"], "metrics": {"score": 1}} for r in records}
        return {"score": 1}
    monkeypatch.setattr(Evaluator, "evaluate", evaluate)
    optimize = Mock(side_effect=AssertionError("Evaluation must not optimize"))
    monkeypatch.setattr(WorkFlowMiproOptimizer, "__init__", optimize)
    state = {"task_id": "eval-only", "created_at": evolve_api._utcnow()}
    monkeypatch.setitem(evolve_api._tasks, "eval-only", state)
    monkeypatch.setattr(evolve_api, "_task_dir", lambda _: tmp_path)
    graph = {"id": "g", "goal": "test", "tasks": [{"name": "a", "description": "Evaluate text", "prompt": "Original prompt", "inputs": [{"name": "text", "type": "string"}], "outputs": [{"name": "answer", "type": "string"}]}], "edges": []}
    evolve_api._execute_evolve("eval-only", graph, "exact_match", evolve_api.resolve_params({"mode": "evaluate"}, 3, ["a"]), tmp_path)
    assert state["status"] == "done", state.get("error")
    assert len(state["baseline"]["records"]) == 3
    assert not state.get("optimized_graph")
    optimize.assert_not_called()
