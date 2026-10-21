"""The workflow assistant creates, reads, updates and deletes canvas Chat Agents.

Every change comes back as an `agent_actions` entry for the frontend; what can
be checked without writing (memory scope, tools, skills, value ranges) is
checked here and a failure answers back to the model with the valid choices.
"""

import json

import pytest

from conftest import make_graph, make_task

HELPER = "cd" * 16


@pytest.fixture
def canvas(tmp_path, monkeypatch):
    from backend.api import mem0_service
    from backend.features.chat import agent_ops
    monkeypatch.setattr(mem0_service, "ROOT", tmp_path / "mem0")
    graph = make_graph([make_task("detect", outputs=["finding"], use_long_term_memory=True)],
                       id="g1", project_id="p1")
    notes = mem0_service.create_space(graph, "Team Notes")
    mem0_service.create_space({"id": "other", "project_id": "p2"}, "Foreign")
    monkeypatch.setattr(agent_ops, "tool_catalog", lambda: [
        {"name": "SearchToolkit", "description": "Web search", "available": True,
         "tools": [{"name": "web_search", "description": "Search the web"},
                   {"name": "fetch_page", "description": "Fetch a web page"}]},
        {"name": "PythonToolkit", "description": "Python", "available": True,
         "tools": [{"name": "python_exec", "description": "Run Python"}]}])
    monkeypatch.setattr(agent_ops, "skill_catalog", lambda: [
        {"name": "style_guide", "description": "House style"}, {"name": "rubric", "description": "Grading"}])
    graph["canvas_agents"] = [{"id": HELPER, "name": "Helper", "engine": "langgraph", "provider": None,
                               "instructions": "Help.", "max_steps": 20, "timeout": 300,
                               "toolkits": None, "tools": None, "skill_names": [], "memories": [],
                               "x": 400, "y": 250}]
    return graph, notes["id"]


def apply(graph, operations):
    from backend.api import chat_api
    return chat_api.apply_operations(graph, operations, "g1")


def errors(result):
    return [o["result"] for o in result["observations"] if o["op"] == "operation_error"]


class TestCreate:
    def test_minimal_create_returns_an_action_with_a_chosen_id(self, canvas):
        graph, _ = canvas
        result = apply(graph, [{"op": "create_agent", "name": "Researcher"}])
        (action,) = result["agent_actions"]
        assert action["action"] == "create" and len(action["id"]) == 32
        assert action["settings"] == {"name": "Researcher"}
        assert action["summary"] == "created Chat Agent 'Researcher'"
        assert [a["name"] for a in result["canvas_agents"]] == ["Helper", "Researcher"]

    def test_create_with_memory_tools_and_skills_then_address_it_by_name(self, canvas):
        graph, space = canvas
        result = apply(graph, [
            {"op": "create_agent", "name": "Researcher", "instructions": "Dig.", "max_steps": 30,
             "tools": ["web search"], "skills": ["Style Guide"], "memories": ["team notes"]},
            {"op": "connect_agent_memory", "agent": "researcher", "memory": "detect memory"},
            {"op": "inspect_agent", "name": "Researcher"},
        ])
        assert errors(result) == []
        create, connect = result["agent_actions"]
        assert create["settings"] == {"name": "Researcher", "instructions": "Dig.", "max_steps": 30,
                                      "tools": ["web_search"], "skill_names": ["style_guide"],
                                      "memories": [{"space_id": space, "read": True, "write": False}]}
        assert connect["id"] == create["id"]
        assert connect["summary"] == "connected 'Researcher' to memory 'detect memory' (reads)"
        inspected = result["observations"][-1]["result"]
        assert [m["name"] for m in inspected["memories"]] == ["Team Notes", "detect memory"]
        assert inspected["tools"] == ["web_search"] and inspected["skill_names"] == ["style_guide"]

    def test_a_taken_name_gets_a_suffix(self, canvas):
        graph, _ = canvas
        result = apply(graph, [{"op": "create_agent", "name": "helper"}])
        assert result["agent_actions"][0]["settings"]["name"] == "helper 2"

    def test_a_memory_out_of_scope_is_refused_with_the_reachable_ones(self, canvas):
        graph, _ = canvas
        result = apply(graph, [{"op": "create_agent", "name": "R", "memories": ["Foreign"]}])
        (error,) = errors(result)
        assert result["agent_actions"] == [] and len(result["canvas_agents"]) == 1
        assert "Team Notes" in error["error"] and "detect memory" in error["error"]
        assert {c["id"] for c in error["candidates"]} <= {"mem:detect", f"mem:space:{canvas[1]}"}

    def test_out_of_range_settings_are_refused(self, canvas):
        graph, _ = canvas
        result = apply(graph, [{"op": "create_agent", "name": "R", "max_steps": 500}])
        assert "max_steps" in errors(result)[0]["error"] and result["agent_actions"] == []


class TestUpdate:
    def test_patch_replaces_lists_and_records_a_summary(self, canvas):
        graph, space = canvas
        result = apply(graph, [{"op": "update_agent", "name": "helper", "patch": {
            "provider": "openai", "tools": ["PythonToolkit"], "memories": [{"memory": "Team Notes", "write": True}]}}])
        (action,) = result["agent_actions"]
        assert action["patch"] == {"provider": "openai", "tools": ["python_exec"],
                                   "memories": [{"space_id": space, "read": True, "write": True}]}
        assert action["summary"] == "updated Chat Agent 'Helper' (provider, tools, memories)"
        assert result["canvas_agents"][0]["provider"] == "openai"

    def test_writing_a_workflow_memory_is_refused(self, canvas):
        graph, _ = canvas
        result = apply(graph, [{"op": "connect_agent_memory", "agent": "Helper", "memory": "detect memory",
                                "write": True}])
        assert "cannot be written" in errors(result)[0]["error"] and result["agent_actions"] == []

    def test_an_unknown_memory_names_the_reachable_ones(self, canvas):
        graph, _ = canvas
        result = apply(graph, [{"op": "connect_agent_memory", "agent": "Helper", "memory": "zzz"}])
        (error,) = errors(result)
        assert "Memories this canvas can connect: Team Notes" in error["error"]
        assert error["candidates"]

    def test_connect_then_disconnect(self, canvas):
        graph, space = canvas
        result = apply(graph, [{"op": "connect_agent_memory", "agent": "Helper", "memory": "Team Notes",
                                "write": True},
                               {"op": "disconnect_agent_memory", "agent": "Helper", "memory": "team notes",
                                "direction": "write"},
                               {"op": "disconnect_agent_memory", "agent": "Helper", "memory": "Team Notes"}])
        connect, drop_write, drop = result["agent_actions"]
        assert connect["patch"] == {"memories": [{"space_id": space, "read": True, "write": True}]}
        assert connect["summary"] == "connected 'Helper' to memory 'Team Notes' (reads and writes)"
        assert drop_write["patch"] == {"memories": [{"space_id": space, "read": True, "write": False}]}
        assert drop["patch"] == {"memories": []}
        assert drop["summary"] == "disconnected 'Helper' from memory 'Team Notes'"

    def test_disconnecting_what_is_not_connected_says_so(self, canvas):
        graph, _ = canvas
        result = apply(graph, [{"op": "disconnect_agent_memory", "agent": "Helper", "memory": "Team Notes"}])
        assert "not connected to any memory" in errors(result)[0]["error"]

    def test_tools(self, canvas):
        graph, _ = canvas
        result = apply(graph, [{"op": "add_agent_tool", "agent": "Helper", "tool": "web_search"}])
        assert result["agent_actions"] == [] and "already has every available tool" in result["notes"][-1]
        result = apply(graph, [{"op": "remove_agent_tool", "agent": "Helper", "tool": "PythonToolkit"},
                               {"op": "add_agent_tool", "agent": "Helper", "tool": "python exec"}])
        remove, add = result["agent_actions"]
        assert remove["patch"] == {"tools": ["web_search", "fetch_page"]}
        assert add["patch"] == {"tools": ["web_search", "fetch_page", "python_exec"]}
        assert add["summary"] == "gave 'Helper' the tool python_exec"
        missing = apply(graph, [{"op": "add_agent_tool", "agent": "Helper", "tool": "teleport"}])
        assert "among the available tools" in errors(missing)[0]["error"]
        assert errors(missing)[0]["candidates"]

    def test_skills(self, canvas):
        graph, _ = canvas
        result = apply(graph, [{"op": "add_agent_skill", "agent": "Helper", "skill": "rubric"},
                               {"op": "remove_agent_skill", "agent": "Helper", "skill": "rubric"}])
        assert [a["patch"] for a in result["agent_actions"]] == [{"skill_names": ["rubric"]}, {"skill_names": []}]
        missing = apply(graph, [{"op": "add_agent_skill", "agent": "Helper", "skill": "zzz"}])
        assert "among the skills" in errors(missing)[0]["error"]

    def test_an_unknown_agent_lists_the_candidates(self, canvas):
        graph, _ = canvas
        result = apply(graph, [{"op": "update_agent", "name": "Nobody", "patch": {"name": "x"}}])
        (error,) = errors(result)
        assert "nothing on the canvas matches 'Nobody'" in error["error"]
        assert error["candidates"][0]["id"] == "chat:" + HELPER

    def test_delete_of_a_loose_match_needs_confirmation(self, canvas):
        graph, _ = canvas
        result = apply(graph, [{"op": "delete_agent", "name": "Helpr"}])
        assert result["agent_actions"] == [] and "confirm" in errors(result)[0]["error"]


class TestRead:
    def test_inspect_and_list_name_the_memories(self, canvas):
        graph, space = canvas
        graph["canvas_agents"][0]["memories"] = [{"space_id": space, "read": True, "write": True}]
        result = apply(graph, [{"op": "inspect_agent", "name": "Helper"}, {"op": "list_agents"}])
        inspected, listed = (o["result"] for o in result["observations"])
        assert inspected["memories"] == [{"id": f"mem:space:{space}", "name": "Team Notes", "kind": "mem0",
                                          "read": True, "write": True}]
        assert inspected["tools"] == "all available tools" and inspected["id"] == "chat:" + HELPER
        assert listed[0]["memories"][0]["name"] == "Team Notes"
        assert result["agent_actions"] == []

    def test_the_prompt_lists_the_operations_and_agent_settings(self, canvas):
        from backend.api import chat_api
        graph, space = canvas
        graph["canvas_agents"][0]["memories"] = [{"space_id": space, "read": True}]
        prompt = chat_api._build_system_prompt(graph, graph["canvas_agents"])
        for op in ("create_agent", "update_agent", "connect_agent_memory", "disconnect_agent_memory",
                   "add_agent_tool", "add_agent_skill", "inspect_agent", "list_agents", "delete_agent"):
            assert f'"op": "{op}"' in prompt, op
        assert '"name": "Team Notes"' in prompt and '"tools": "all available tools"' in prompt
        summary = json.loads(chat_api._canvas_summary(graph, graph["canvas_agents"]))
        assert summary["canvas_agents"][0]["memories"][0]["name"] == "Team Notes"


def test_each_step_sees_the_agents_as_changed(canvas, monkeypatch):
    from backend.api import chat_api, graphs
    graph, _ = canvas
    monkeypatch.setattr(graphs, "graph_exists", lambda _: True)
    monkeypatch.setattr(chat_api, "_validate", lambda g: [])
    calls, script = [], [
        {"reply": "", "operations": [{"op": "create_agent", "name": "Scout", "memories": ["Team Notes"]},
                                     {"op": "list_agents"}]},
        {"reply": "Created.", "operations": []}]
    monkeypatch.setattr(chat_api, "_ask", lambda m: calls.append(m) or json.dumps(script.pop(0)))
    result = chat_api.chat("g1", {"graph": graph, "message": "add a chat agent Scout connected to Team Notes"})
    assert [a["action"] for a in result["agent_actions"]] == ["create"]
    step = calls[1][-1]["content"].split("Current canvas")[1]
    assert '"name": "Scout"' in step and "Team Notes" in step


def test_the_store_accepts_the_chosen_id_once(studio_data):
    from fastapi.testclient import TestClient
    from backend.api import app as studio_app, graphs
    client = TestClient(studio_app.app)
    gid = graphs.create_graph("Agents", "")["id"]
    chosen = "ef" * 16
    made = client.post(f"/api/graphs/{gid}/agents", json={"id": chosen, "name": "Scout"})
    assert made.status_code == 201 and made.json()["id"] == chosen
    assert client.post(f"/api/graphs/{gid}/agents", json={"id": chosen, "name": "Again"}).status_code == 409
    assert client.post(f"/api/graphs/{gid}/agents", json={"id": "nope", "name": "Bad"}).status_code == 422
    assert len(client.post(f"/api/graphs/{gid}/agents", json={"name": "Auto"}).json()["id"]) == 32
