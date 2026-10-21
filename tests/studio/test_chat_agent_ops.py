"""The workflow assistant creates, reads, updates and deletes Chat Agents.

A Chat Agent is a node of kind "chat_agent": every change is an edit of the
graph and comes back as an ordinary canvas operation (add_node / update_node
/ rename_node / delete_node), replayable and undoable like any other. What
can be checked (memory scope, tools, skills, value ranges) is checked here
and a failure answers back to the model with the valid choices.
"""

import json

import pytest

from conftest import make_graph, make_task

HELPER = "cd" * 16


def helper_node(**extra):
    return {"name": "helper", "kind": "chat_agent", "agent_id": HELPER, "description": "",
            "provider": None, "instructions": "Help.", "max_steps": 20, "timeout": 300,
            "toolkits": None, "tools": None, "skill_names": [], "memories": [], "x": 400, "y": 250,
            **extra}


@pytest.fixture
def canvas(tmp_path, monkeypatch):
    from backend.api import mem0_service
    from backend.features.chat import agent_ops
    monkeypatch.setattr(mem0_service, "ROOT", tmp_path / "mem0")
    graph = make_graph([make_task("detect", outputs=["finding"], use_long_term_memory=True),
                        helper_node()], id="g1", project_id="p1")
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
    return graph, notes["id"]


def apply(graph, operations):
    from backend.api import chat_api
    return chat_api.apply_operations(graph, operations, "g1")


def errors(result):
    return [o["result"] for o in result["observations"] if o["op"] == "operation_error"]


def agents(result):
    return [t for t in result["graph"]["tasks"] if t.get("kind") == "chat_agent"]


def node(result, name):
    return next(t for t in result["graph"]["tasks"] if t["name"] == name)


class TestCreate:
    def test_minimal_create_adds_a_node_with_an_agent_id(self, canvas):
        graph, _ = canvas
        result = apply(graph, [{"op": "create_agent", "name": "Researcher"}])
        (op,) = result["canvas_operations"]
        assert op["op"] == "add_node" and op["summary"] == "created Chat Agent 'researcher'"
        made = node(result, "researcher")
        assert op["task"] == made
        assert made["kind"] == "chat_agent" and len(made["agent_id"]) == 32 and made["agent_id"] != HELPER
        assert made["instructions"] and made["max_steps"] == 20 and made["tools"] is None
        assert [a["name"] for a in agents(result)] == ["helper", "researcher"]
        assert "canvas_agents" not in result and "agent_actions" not in result

    def test_add_node_of_kind_chat_agent_is_the_same(self, canvas):
        graph, space = canvas
        result = apply(graph, [{"op": "add_node", "task": {"kind": "chat_agent", "name": "scout",
                                                           "memories": ["Team Notes"]}}])
        assert errors(result) == []
        assert node(result, "scout")["memories"] == [{"space_id": space, "read": True, "write": False}]
        assert result["canvas_operations"][0]["op"] == "add_node"

    def test_create_with_memory_tools_and_skills_then_address_it_by_name(self, canvas):
        graph, space = canvas
        result = apply(graph, [
            {"op": "create_agent", "name": "Researcher", "instructions": "Dig.", "max_steps": 30,
             "tools": ["web search"], "skills": ["Style Guide"], "memories": ["team notes"]},
            {"op": "connect_agent_memory", "agent": "researcher", "memory": "detect memory"},
            {"op": "inspect_agent", "name": "Researcher"},
        ])
        assert errors(result) == []
        create, connect = result["canvas_operations"]
        assert {k: create["task"][k] for k in ("instructions", "max_steps", "tools", "skill_names", "memories")} == {
            "instructions": "Dig.", "max_steps": 30, "tools": ["web_search"], "skill_names": ["style_guide"],
            "memories": [{"space_id": space, "read": True, "write": False}]}
        assert connect["op"] == "update_node" and connect["name"] == "researcher"
        assert connect["summary"] == "connected 'researcher' to memory 'detect memory' (reads)"
        inspected = result["observations"][-1]["result"]
        assert [m["name"] for m in inspected["memories"]] == ["Team Notes", "detect memory"]
        assert inspected["tools"] == ["web_search"] and inspected["skill_names"] == ["style_guide"]

    def test_a_taken_name_gets_a_suffix(self, canvas):
        graph, _ = canvas
        result = apply(graph, [{"op": "create_agent", "name": "helper"}, {"op": "create_agent", "name": "detect"}])
        assert [a["name"] for a in agents(result)] == ["helper", "helper_2", "detect_2"]
        assert len({a["agent_id"] for a in agents(result)}) == 3

    def test_a_memory_out_of_scope_is_refused_with_the_reachable_ones(self, canvas):
        graph, _ = canvas
        result = apply(graph, [{"op": "create_agent", "name": "R", "memories": ["Foreign"]}])
        (error,) = errors(result)
        assert result["canvas_operations"] == [] and len(agents(result)) == 1
        assert "Team Notes" in error["error"] and "detect memory" in error["error"]
        assert {c["id"] for c in error["candidates"]} <= {"mem:detect", f"mem:space:{canvas[1]}"}

    def test_out_of_range_settings_are_refused(self, canvas):
        graph, _ = canvas
        result = apply(graph, [{"op": "create_agent", "name": "R", "max_steps": 500}])
        assert "max_steps" in errors(result)[0]["error"] and result["canvas_operations"] == []

    def test_a_replayed_add_keeps_its_agent_id(self, canvas):
        graph, _ = canvas
        first = apply(graph, [{"op": "create_agent", "name": "Scout"}])
        replayed = apply(graph, first["canvas_operations"])
        assert node(replayed, "scout")["agent_id"] == node(first, "scout")["agent_id"]


class TestUpdate:
    def test_patch_replaces_lists_and_records_a_summary(self, canvas):
        graph, space = canvas
        result = apply(graph, [{"op": "update_agent", "name": "helper", "patch": {
            "provider": "openai", "tools": ["PythonToolkit"], "memories": [{"memory": "Team Notes", "write": True}]}}])
        (op,) = result["canvas_operations"]
        assert op == {"op": "update_node", "name": "helper", "summary":
                      "updated Chat Agent 'helper' (provider, tools, memories)",
                      "patch": {"provider": "openai", "tools": ["python_exec"],
                                "memories": [{"space_id": space, "read": True, "write": True}]}}
        assert node(result, "helper")["provider"] == "openai"

    def test_update_node_on_a_chat_agent_is_checked_the_same_way(self, canvas):
        graph, _ = canvas
        bad = apply(graph, [{"op": "update_node", "name": "helper", "patch": {"timeout": 1}}])
        assert "timeout" in errors(bad)[0]["error"] and node(bad, "helper")["timeout"] == 300
        good = apply(graph, [{"op": "update_node", "name": "helper", "patch": {"skills": ["rubric"]}}])
        assert node(good, "helper")["skill_names"] == ["rubric"]

    def test_renaming_moves_the_node_and_keeps_its_agent_id(self, canvas):
        graph, _ = canvas
        result = apply(graph, [{"op": "update_agent", "name": "helper", "patch": {"name": "Analyst",
                                                                                   "max_steps": 5}}])
        rename, update = result["canvas_operations"]
        assert rename == {"op": "rename_node", "name": "helper", "new_name": "analyst"}
        assert update["name"] == "analyst" and update["patch"] == {"max_steps": 5}
        assert node(result, "analyst")["agent_id"] == HELPER
        plain = apply(graph, [{"op": "rename_node", "name": "helper", "new_name": "Scout"}])
        assert node(plain, "scout")["agent_id"] == HELPER

    def test_writing_a_workflow_memory_is_refused(self, canvas):
        graph, _ = canvas
        result = apply(graph, [{"op": "connect_agent_memory", "agent": "Helper", "memory": "detect memory",
                                "write": True}])
        assert "cannot be written" in errors(result)[0]["error"] and result["canvas_operations"] == []

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
        connect, drop_write, drop = result["canvas_operations"]
        assert connect["patch"] == {"memories": [{"space_id": space, "read": True, "write": True}]}
        assert connect["summary"] == "connected 'helper' to memory 'Team Notes' (reads and writes)"
        assert drop_write["patch"] == {"memories": [{"space_id": space, "read": True, "write": False}]}
        assert drop["patch"] == {"memories": []}
        assert drop["summary"] == "disconnected 'helper' from memory 'Team Notes'"

    def test_disconnecting_what_is_not_connected_says_so(self, canvas):
        graph, _ = canvas
        result = apply(graph, [{"op": "disconnect_agent_memory", "agent": "Helper", "memory": "Team Notes"}])
        assert "not connected to any memory" in errors(result)[0]["error"]

    def test_tools(self, canvas):
        graph, _ = canvas
        result = apply(graph, [{"op": "add_agent_tool", "agent": "Helper", "tool": "web_search"}])
        assert result["canvas_operations"] == [] and "already has every available tool" in result["notes"][-1]
        result = apply(graph, [{"op": "remove_agent_tool", "agent": "Helper", "tool": "PythonToolkit"},
                               {"op": "add_agent_tool", "agent": "Helper", "tool": "python exec"}])
        remove, add = result["canvas_operations"]
        assert remove["patch"] == {"tools": ["web_search", "fetch_page"]}
        assert add["patch"] == {"tools": ["web_search", "fetch_page", "python_exec"]}
        assert add["summary"] == "gave 'helper' the tool python_exec"
        missing = apply(graph, [{"op": "add_agent_tool", "agent": "Helper", "tool": "teleport"}])
        assert "among the available tools" in errors(missing)[0]["error"]
        assert errors(missing)[0]["candidates"]

    def test_skills(self, canvas):
        graph, _ = canvas
        result = apply(graph, [{"op": "add_agent_skill", "agent": "Helper", "skill": "rubric"},
                               {"op": "remove_agent_skill", "agent": "Helper", "skill": "rubric"}])
        assert [a["patch"] for a in result["canvas_operations"]] == [{"skill_names": ["rubric"]},
                                                                      {"skill_names": []}]
        missing = apply(graph, [{"op": "add_agent_skill", "agent": "Helper", "skill": "zzz"}])
        assert "among the skills" in errors(missing)[0]["error"]

    def test_an_unknown_agent_lists_the_candidates(self, canvas):
        graph, _ = canvas
        result = apply(graph, [{"op": "update_agent", "name": "Nobody", "patch": {"name": "x"}}])
        (error,) = errors(result)
        assert "nothing on the canvas matches 'Nobody'" in error["error"]
        assert error["candidates"][0]["id"] == "helper"

    def test_an_agent_is_found_by_its_old_chat_id(self, canvas):
        graph, _ = canvas
        result = apply(graph, [{"op": "update_agent", "name": "chat:" + HELPER, "patch": {"max_steps": 3}}])
        assert node(result, "helper")["max_steps"] == 3

    def test_delete_of_a_loose_match_needs_confirmation(self, canvas):
        graph, _ = canvas
        result = apply(graph, [{"op": "delete_agent", "name": "Helpr"}])
        assert result["canvas_operations"] == [] and "confirm" in errors(result)[0]["error"]

    def test_delete_agent_and_delete_node_remove_the_node(self, canvas):
        graph, _ = canvas
        for op in ("delete_agent", "delete_node"):
            result = apply(graph, [{"op": op, "name": "helper"}])
            assert agents(result) == []
            assert result["canvas_operations"][0]["op"] == "delete_node"
        # delete_agent never deletes a node that runs.
        assert errors(apply(graph, [{"op": "delete_agent", "name": "detect"}]))

    def test_a_chat_agent_gets_no_edges(self, canvas):
        graph, _ = canvas
        result = apply(graph, [{"op": "add_edge", "source": "detect", "target": "helper"}])
        assert "does not run" in errors(result)[0]["error"] and result["graph"]["edges"] == []


class TestRead:
    def test_inspect_and_list_name_the_memories(self, canvas):
        graph, space = canvas
        graph["tasks"][1]["memories"] = [{"space_id": space, "read": True, "write": True}]
        result = apply(graph, [{"op": "inspect_agent", "name": "Helper"}, {"op": "list_agents"},
                               {"op": "inspect_node", "name": "helper"}])
        inspected, listed, as_node = (o["result"] for o in result["observations"])
        assert inspected["memories"] == [{"id": f"mem:space:{space}", "name": "Team Notes", "kind": "mem0",
                                          "read": True, "write": True}]
        assert inspected["tools"] == "all available tools" and inspected["agent_id"] == HELPER
        assert as_node == inspected
        assert listed[0]["memories"][0]["name"] == "Team Notes"
        assert result["canvas_operations"] == []

    def test_the_prompt_lists_the_operations_and_agent_settings(self, canvas):
        from backend.api import chat_api
        graph, space = canvas
        graph["tasks"][1]["memories"] = [{"space_id": space, "read": True}]
        prompt = chat_api._build_system_prompt(graph)
        for op in ("create_agent", "update_agent", "connect_agent_memory", "disconnect_agent_memory",
                   "add_agent_tool", "add_agent_skill", "inspect_agent", "list_agents", "delete_agent"):
            assert f'"op": "{op}"' in prompt, op
        assert '"kind": "chat_agent"' in prompt and "does NOT run" in prompt
        assert '"name": "Team Notes"' in prompt and '"tools": "all available tools"' in prompt
        summary = json.loads(chat_api._canvas_summary(graph))
        assert summary["tasks"][1]["memories"][0]["name"] == "Team Notes"


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
    assert [o["op"] for o in result["canvas_operations"]] == ["add_node"]
    assert result["applied"] and any(t["name"] == "scout" for t in result["graph"]["tasks"])
    assert "agent_actions" not in result
    step = calls[1][-1]["content"].split("Current canvas")[1]
    assert '"name": "scout"' in step and "Team Notes" in step


def test_assistant_agent_edits_replay_onto_a_changed_canvas(canvas, monkeypatch):
    """The turn's Chat Agent edits are canvas operations: replayed onto a
    canvas the person changed meanwhile, as any other edit."""
    from backend.api import chat_api, graphs
    graph, space = canvas
    monkeypatch.setattr(graphs, "graph_exists", lambda _: True)
    turn = apply(graph, [{"op": "create_agent", "name": "Scout"},
                         {"op": "connect_agent_memory", "agent": "scout", "memory": "Team Notes"}])
    moved = {**graph, "goal": "changed meanwhile"}
    replayed = chat_api.replay("g1", {"graph": moved, "operations": turn["canvas_operations"]})
    assert replayed["applied"]
    scout = next(t for t in replayed["graph"]["tasks"] if t["name"] == "scout")
    assert scout["memories"] == [{"space_id": space, "read": True, "write": False}]
    assert scout["agent_id"] == node(turn, "scout")["agent_id"] and replayed["graph"]["goal"] == "changed meanwhile"


def test_the_canvas_snapshot_reaches_its_projects_shared_memory(studio_data, tmp_path, monkeypatch):
    """The canvas the frontend sends has no project_id or task_id (the snapshot
    carries only what the canvas draws); its shared memories are scoped by the
    project, so the assistant found none and could not connect any of them."""
    from backend.api import graphs, mem0_service
    monkeypatch.setattr(mem0_service, "ROOT", tmp_path / "mem0")
    stored = graphs.create_graph("Scoped", "")
    graphs.save_graph(stored["id"], {**graphs.load_graph(stored["id"]), "project_id": "p1"})
    space = mem0_service.create_space({"id": stored["id"], "project_id": "p1"}, "Team Notes")
    snapshot = {"id": stored["id"], "name": "Scoped", "goal": "", "tasks": [helper_node()], "edges": [],
                "memory_resources": [{"space_id": space["id"], "name": "Team Notes"}]}

    from backend.api import chat_api
    result = chat_api.apply_operations(snapshot, [
        {"op": "connect_agent_memory", "agent": "Helper", "memory": "Team Notes"},
        {"op": "list_memories"}], stored["id"])

    assert errors(result) == []
    (op,) = result["canvas_operations"]
    assert op["patch"]["memories"] == [{"space_id": space["id"], "read": True, "write": False}]
    listed = next(o["result"] for o in result["observations"] if o["op"] == "list_memories")
    assert [m["name"] for m in listed] == ["Team Notes"]
