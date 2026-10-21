"""Chat Agents are nodes of the workflow graph (kind "chat_agent").

Saved, loaded, renamed and copied with the graph; never part of a run, a
plan, an evaluation or the standalone export; conversations keyed by the
node's agent_id in the harness store. Agents an earlier version kept as
harness rows become nodes of their workflow, once.
"""
import json

import pytest
from fastapi.testclient import TestClient

from backend.api import graphs, run_plan
from backend.features.agents import chat_agents, harness_api
from conftest import make_task

AGENT = "a7" * 16


def chat_agent(name="helper", **settings):
    return {"name": name, "kind": "chat_agent", "agent_id": AGENT, **settings}


@pytest.fixture
def harness_store(tmp_path, monkeypatch):
    monkeypatch.setattr(harness_api, "data_path", lambda *p: tmp_path.joinpath("harness-data", *p))
    return tmp_path


def _client():
    from backend.api import app as studio_app
    return TestClient(studio_app.app)


def _row(id, owner, kind, body, parent=""):
    with harness_api.database() as db:
        harness_api.put(db, id, owner, kind, parent, body)


def _kinds(owner):
    with harness_api.database() as db:
        return dict(db.execute("SELECT id, kind FROM objects WHERE owner=?", (owner,)).fetchall())


class TestTheNode:
    def test_save_and_load_round_trip_fills_defaults(self, studio_data):
        gid = graphs.create_graph("Round Trip", "")["id"]
        saved = graphs.save_graph(gid, {"name": "Round Trip", "edges": [], "tasks": [
            make_task("a", outputs=["x"]),
            chat_agent(instructions="Be brief.", memories=[{"space_id": "b" * 32, "write": True}],
                       x=12.0, y=7.5, unknown_field=1)]})
        node = graphs.load_graph(gid)["tasks"][1]
        assert node == saved["tasks"][1]
        assert node["agent_id"] == AGENT and node["instructions"] == "Be brief."
        assert node["max_steps"] == 20 and node["timeout"] == 300 and node["tools"] is None
        assert node["memories"] == [{"space_id": "b" * 32, "read": True, "write": True}]
        assert (node["x"], node["y"]) == (12, 7.5) and "unknown_field" not in node

    def test_every_node_gets_an_agent_id_of_its_own(self, studio_data):
        gid = graphs.create_graph("Ids", "")["id"]
        saved = graphs.save_graph(gid, {"name": "Ids", "edges": [], "tasks": [
            chat_agent("one"), chat_agent("two"), {"name": "three", "kind": "chat_agent"}]})
        ids = [t["agent_id"] for t in saved["tasks"]]
        assert ids[0] == AGENT and len(set(ids)) == 3 and all(len(i) == 32 for i in ids)

    def test_invalid_settings_and_data_edges_are_refused(self, studio_data):
        gid = graphs.create_graph("Refused", "")["id"]
        with pytest.raises(graphs.GraphValidationError, match="max_steps"):
            graphs.save_graph(gid, {"name": "Refused", "edges": [], "tasks": [chat_agent(max_steps=0)]})
        edged = {"name": "Refused", "tasks": [make_task("a", outputs=["x"]), chat_agent()],
                 "edges": [{"source": "a", "target": "helper", "control_only": True}]}
        with pytest.raises(graphs.GraphValidationError, match="cannot have data edges"):
            graphs.validate_graph(edged)
        with pytest.raises(graphs.GraphValidationError, match="cannot have data edges"):
            graphs.save_graph(gid, edged)

    def test_the_put_endpoint_saves_it_and_returns_it(self, studio_data):
        client = _client()
        gid = graphs.create_graph("Through Put", "")["id"]
        body = {"name": "Through Put", "goal": "", "tasks": [make_task("a", outputs=["x"]), chat_agent()],
                "edges": [], "flow_version": 3}
        saved = client.put(f"/api/graphs/{gid}", json=body)
        assert saved.status_code == 200, saved.text
        assert [t["name"] for t in saved.json()["tasks"]] == ["a", "helper"]
        assert saved.json()["workflow_inputs"] == []


class TestNotPartOfRuns:
    def graph(self):
        return {"id": "g", "goal": "", "flow_version": 3,
                "tasks": [make_task("a", inputs=["q"], outputs=["x"]), chat_agent()], "edges": []}

    def test_validation_and_the_plan_leave_it_out(self):
        ordered, inputs = graphs.validate_graph(self.graph())
        assert [t["name"] for t in ordered] == ["a"] and [i["name"] for i in inputs] == ["q"]
        plan = run_plan.compile_plan(self.graph())
        assert [n["name"] for n in plan["nodes"]] == ["a"] and "helper" not in plan["skipped"]

    def test_editing_it_does_not_make_a_plan_stale(self):
        changed = self.graph()
        changed["tasks"][1]["instructions"] = "Something else"
        assert run_plan.compile_plan(changed)["plan_id"] == run_plan.compile_plan(self.graph())["plan_id"]

    def test_a_canvas_of_only_chat_agents_has_nothing_to_run(self):
        with pytest.raises(graphs.GraphValidationError, match="Nothing would run"):
            run_plan.compile_plan({"goal": "", "flow_version": 3, "tasks": [chat_agent()], "edges": []})

    def test_a_run_never_sees_it(self, monkeypatch):
        from backend.api import runner
        started = {}
        monkeypatch.setattr(runner, "_execute_run_scoped", lambda run_id, graph, inputs: started.update(graph=graph))
        run_id = runner.start_run(self.graph(), {"q": "?"}, background=False)
        assert [t["name"] for t in started["graph"]["tasks"]] == ["a"]
        assert [n["name"] for n in runner.get_run(run_id)["nodes"]] == ["a"]

    def test_parked_and_workflow_only(self):
        graph = self.graph()
        assert graphs.parked_task_names(graph["tasks"], []) == {"helper"}
        assert [t["name"] for t in chat_agents.workflow_only(graph)["tasks"]] == ["a"]

    def test_the_standalone_export_leaves_it_out_and_graph_json_keeps_it(self, studio_data):
        from backend.api import export_api
        files, _ = export_api.project_files({**self.graph(), "name": "Exported"}, include_vendor=False)
        assert "helper" not in files["workflow.py"]
        assert [t["name"] for t in json.loads(files["graph.json"])["tasks"]] == ["a", "helper"]


class TestChatting:
    def test_a_message_uses_the_saved_node_and_an_unsaved_one_is_refused(self, studio_data, harness_store,
                                                                       monkeypatch):
        submitted = []
        monkeypatch.setattr(harness_api._pool, "submit", lambda *a: submitted.append(a))
        monkeypatch.setattr(harness_api.harness, "make_model", lambda provider: object())
        monkeypatch.setattr(harness_api, "_running", {})
        client = _client()
        gid = graphs.create_graph("Chatting", "")["id"]
        graphs.save_graph(gid, {"name": "Chatting", "edges": [], "tasks": [chat_agent(instructions="Saved.")]})
        root = f"/api/graphs/{gid}/agents/{AGENT}/sessions"
        session = client.post(root).json()
        assert client.post(f"{root}/{session['id']}/messages", json={"message": "hi"}).status_code == 202
        assert submitted[-1][2]["instructions"] == "Saved." and submitted[-1][2]["id"] == AGENT
        other = "f0" * 16
        refused = client.post(f"/api/graphs/{gid}/agents/{other}/sessions")
        assert refused.status_code == 409 and "Save the workflow" in refused.json()["detail"]


class TestMigration:
    def legacy(self, gid, owner=None, **rows):
        owner = owner or gid
        _row(AGENT, owner, "agent", {"id": AGENT, "name": "Chat Agent 1", "engine": "langgraph",
                                     "instructions": "Old.", "max_steps": 9, "timeout": 300,
                                     "provider": None, "toolkits": None, "tools": ["double"],
                                     "skill_names": [], "memories": [{"memory_id": "mem:a", "read": True,
                                                                      "write": False}],
                                     "x": 410.0, "y": 260.0})
        _row("s" * 32, owner, "session", {"id": "s" * 32, "created_at": 1, "status": "idle",
                                          "messages": [{"role": "user", "content": "earlier"}],
                                          "events": [], "error": None}, parent=AGENT)

    def test_rows_become_nodes_once_and_sessions_stay_reachable(self, studio_data, harness_store):
        gid = graphs.create_graph("Migrated", "")["id"]
        graphs.save_graph(gid, {"name": "Migrated", "edges": [],
                                "tasks": [make_task("a", outputs=["x"]), make_task("chat_agent_1")]})
        self.legacy(gid)
        _row("c" * 32, gid, "archived-agent", {"id": "c" * 32, "name": "Removed"})
        assert harness_api.migrate_agents(gid) == 1
        tasks = graphs.load_graph(gid)["tasks"]
        node = tasks[-1]
        assert [t["name"] for t in tasks] == ["a", "chat_agent_1", "chat_agent_1_2"]   # name taken: suffix
        assert node["kind"] == "chat_agent" and node["agent_id"] == AGENT
        assert (node["instructions"], node["max_steps"], node["tools"]) == ("Old.", 9, ["double"])
        assert node["memories"] == [{"memory_id": "mem:a", "read": True, "write": False}]
        assert (node["x"], node["y"]) == (410, 260)
        assert _kinds(gid) == {AGENT: "migrated-agent", "c" * 32: "archived-agent", "s" * 32: "session"}
        assert [s["id"] for s in harness_api.list_sessions(gid, AGENT)["sessions"]] == ["s" * 32]
        assert harness_api.migrate_agents(gid) == 0
        assert len(graphs.load_graph(gid)["tasks"]) == 3

    def test_an_interrupted_migration_is_finished_without_a_second_node(self, studio_data, harness_store):
        gid = graphs.create_graph("Interrupted", "")["id"]
        graphs.save_graph(gid, {"name": "Interrupted", "edges": [],
                                "tasks": [chat_agent("chat_agent_1")]})   # saved, row not yet marked
        self.legacy(gid)
        assert harness_api.migrate_agents(gid) == 0
        assert [t["name"] for t in graphs.load_graph(gid)["tasks"]] == ["chat_agent_1"]
        assert _kinds(gid)[AGENT] == "migrated-agent"

    def test_rows_under_a_task_id_and_on_open_and_at_startup(self, studio_data, harness_store):
        gid = graphs.create_graph("Opened", "")["id"]
        graphs.save_graph(gid, {"name": "Opened", "edges": [], "tasks": [], "task_id": "t-opened"})
        self.legacy(gid, owner="t-opened")
        opened = _client().get(f"/api/graphs/{gid}").json()
        assert [t["agent_id"] for t in opened["tasks"]] == [AGENT]
        other = graphs.create_graph("At Startup", "")["id"]
        _row("e" * 32, other, "agent", {"id": "e" * 32, "name": "Second"})
        _row("9" * 32, "gone.deleted-1", "agent", {"id": "9" * 32, "name": "Of a deleted workflow"})
        assert harness_api.migrate_all() == 1
        assert [t["name"] for t in graphs.load_graph(other)["tasks"]] == ["second"]
        assert _kinds("gone.deleted-1") == {"9" * 32: "agent"}

    def test_a_row_with_settings_that_no_longer_validate_is_left_alone(self, studio_data, harness_store):
        gid = graphs.create_graph("Broken Row", "")["id"]
        _row(AGENT, gid, "agent", {"id": AGENT, "name": "Bad", "max_steps": 0})
        assert harness_api.migrate_agents(gid) == 0
        assert graphs.load_graph(gid)["tasks"] == [] and _kinds(gid)[AGENT] == "agent"


def test_generating_a_workflow_keeps_the_chat_agents(monkeypatch):
    from backend.api import chat_api
    monkeypatch.setattr(chat_api, "_workflow_from_goal", lambda goal, on_stage=None: {
        "tasks": [make_task("helper", outputs=["x"])], "edges": []})
    graph = {"id": "gen", "goal": "", "flow_version": 3, "tasks": [make_task("old"), chat_agent()], "edges": []}
    job_id = chat_api.start_generation(graph, "Summarise the news of the day")
    import time
    for _ in range(200):
        job = chat_api._jobs[job_id]
        if job["status"] != "running":
            break
        time.sleep(0.01)
    assert job["status"] == "done", job
    tasks = job["graph"]["tasks"]
    assert [t["name"] for t in tasks] == ["helper", "helper_2"]
    assert tasks[1]["kind"] == "chat_agent" and tasks[1]["agent_id"] == AGENT
