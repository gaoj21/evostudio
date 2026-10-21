"""The workflow assistant finds what the person named, and never pretends.

A demo failed with "找不到这个节点" when the user asked to delete a node or a
canvas Chat Agent by a slightly different name. These cover the resolver's
tiers, the Chat Agent operations, misses that report candidates, partial or
missing operations, and replaying a turn onto a canvas edited meanwhile.
"""

import json

import pytest

from conftest import make_graph, make_task

AGENT_ID = "ab" * 16


def apply(graph, operations, **kw):
    from backend.api import chat_api
    return chat_api.apply_operations(graph, operations, None, **kw)


def canvas():
    graph = make_graph(
        [make_task("risk_detector", outputs=["finding"], description="检测新闻中的风险信号"),
         make_task("summarize", inputs=["finding"], outputs=["report"], description="Summarise findings")],
        edges=[("risk_detector", "summarize")])
    graph["canvas_agents"] = [{"id": AGENT_ID, "name": "Research Chat", "instructions": "Answer questions"}]
    return graph


def names(result):
    return [t["name"] for t in result["graph"]["tasks"]]


class TestResolverTiers:
    @pytest.mark.parametrize("query", ["Risk Detector", "risk detector", "risk_detector ", "RISK-DETECTOR"])
    def test_spelling_variants_resolve_and_say_so(self, query):
        result = apply(canvas(), [{"op": "delete_node", "name": query}])
        assert names(result) == ["summarize"]
        assert result["graph"]["edges"] == []
        assert result["canvas_operations"] == [{"op": "delete_node", "name": "risk_detector"}]
        if query.strip() != "risk_detector":
            assert any("resolved" in n and "risk_detector" in n for n in result["notes"])

    def test_description_alias_in_chinese_resolves_a_non_destructive_edit(self):
        result = apply(canvas(), [{"op": "update_node", "name": "风险信号", "patch": {"prompt": "new"}}])
        assert result["graph"]["tasks"][0]["prompt"] == "new"
        assert any("by description" in n for n in result["notes"])

    def test_loose_matches_are_not_deleted_without_confirmation(self):
        for query in ("风险信号", "风险检测", "detector"):
            result = apply(canvas(), [{"op": "delete_node", "name": query}])
            assert names(result) == ["risk_detector", "summarize"], query
            error = result["observations"][0]["result"]
            assert "confirm" in error["error"] and error["candidates"][0]["id"] == "risk_detector"

    def test_ambiguity_lists_candidates_and_changes_nothing(self):
        graph = make_graph([make_task("risk_a", description="score risk"),
                            make_task("risk_b", description="explain risk")])
        result = apply(graph, [{"op": "update_node", "name": "risk", "patch": {"prompt": "x"}}])
        error = result["observations"][0]["result"]
        assert "several" in error["error"]
        assert {c["id"] for c in error["candidates"]} == {"risk_a", "risk_b"}
        assert all(t["prompt"] != "x" for t in result["graph"]["tasks"])

    def test_a_miss_reports_the_closest_candidates(self):
        result = apply(canvas(), [{"op": "inspect_node", "name": "zzz"}])
        error = result["observations"][0]["result"]
        assert "nothing on the canvas matches 'zzz'" in error["error"]
        candidate = {c["id"]: c for c in error["candidates"]}
        assert set(candidate) == {"risk_detector", "summarize", "chat:" + AGENT_ID}
        assert candidate["chat:" + AGENT_ID] == {"id": "chat:" + AGENT_ID, "name": "Research Chat",
                                                 "kind": "chat_agent", "description": "Answer questions"}

    def test_resolver_tiers_directly(self):
        from backend.features.chat import node_resolver as r
        candidates = r.task_candidates([{"name": "risk_detector", "description": "检测新闻中的风险信号"}])
        assert r.resolve("risk_detector", candidates)["tier"] == r.EXACT
        assert r.resolve("Risk Detector", candidates)["tier"] == r.NORMALIZED
        assert r.resolve("风险", candidates)["tier"] == r.ALIAS
        assert r.resolve("风险检测", candidates)["tier"] == r.FUZZY
        assert r.resolve("风险检测", candidates, destructive=True)["status"] == "confirm"
        assert r.resolve("Risk Detector", candidates, destructive=True)["status"] == "ok"
        assert r.resolve("unrelated", candidates)["status"] == "missing"


class TestChatAgents:
    def test_the_prompt_lists_canvas_agents(self):
        from backend.api import chat_api
        prompt = chat_api._build_system_prompt(canvas(), canvas()["canvas_agents"])
        assert f'"id": "chat:{AGENT_ID}"' in prompt and '"name": "Research Chat"' in prompt
        assert "delete_agent" in prompt

    @pytest.mark.parametrize("op", [
        {"op": "delete_agent", "name": "research chat"},
        {"op": "delete_node", "name": "Research Chat"},
        {"op": "delete_agent", "name": "chat:" + AGENT_ID},
    ])
    def test_delete_agent_returns_an_action_for_the_canvas(self, op):
        result = apply(canvas(), [op])
        assert result["agent_actions"] == [{"action": "remove", "id": AGENT_ID, "name": "Research Chat",
                                     "summary": "removed Chat Agent 'Research Chat'"}]
        assert result["canvas_agents"] == []
        assert names(result) == ["risk_detector", "summarize"]
        assert "canvas_agents" not in result["graph"]
        assert result["canvas_operations"] == []

    def test_update_and_rename_agent(self):
        result = apply(canvas(), [
            {"op": "update_agent", "name": "Research Chat", "patch": {"instructions": "Be brief", "engine": "x"}},
            {"op": "rename_node", "name": "research chat", "new_name": "Analyst"},
        ])
        assert result["agent_actions"] == [
            {"action": "update", "id": AGENT_ID, "patch": {"instructions": "Be brief"},
             "summary": "updated Chat Agent 'Research Chat' (instructions)"},
            {"action": "update", "id": AGENT_ID, "patch": {"name": "Analyst"},
             "summary": "renamed Chat Agent 'Research Chat' to 'Analyst'"},
        ]
        assert any("ignored unsupported field(s) engine" in n for n in result["notes"])
        assert result["canvas_agents"][0]["name"] == "Analyst"

    def test_workflow_operations_do_not_touch_agents(self):
        result = apply(canvas(), [{"op": "update_node", "name": "Research Chat", "patch": {"prompt": "x"}}])
        assert "Chat Agent" in result["notes"][-1] and result["agent_actions"] == []


def test_delete_edge_miss_reports_the_existing_edges():
    result = apply(canvas(), [{"op": "delete_edge", "source": "summarize", "target": "risk_detector"}])
    error = result["observations"][0]["result"]
    assert "no edge summarize -> risk_detector" in error["error"]
    assert error["candidates"] == [{"source": "risk_detector", "target": "summarize"}]
    assert len(result["graph"]["edges"]) == 1
    assert result["notes"] == [error["error"]]


def test_delete_edge_resolves_endpoint_names():
    result = apply(canvas(), [{"op": "delete_edge", "source": "Risk Detector", "target": "Summarize"}])
    assert result["graph"]["edges"] == []
    assert result["canvas_operations"] == [{"op": "delete_edge", "source": "risk_detector", "target": "summarize"}]


class TestModelReplies:
    def test_a_truncated_reply_is_not_executed_in_part(self):
        from backend.api import model_json
        with pytest.raises(model_json.MalformedJSON):
            model_json.extract_json('{"reply":"ok","operations":[{"op":"delete_node","name":"a"},{"op":"delete_node","name":"b"')
        with pytest.raises(model_json.MalformedJSON):
            model_json.extract_json('{"reply": "ok", "operations": [{"op": "delete_node", "name": "a"}], }')
        assert model_json.extract_json('Use {news} in the prompt') == {"reply": "Use {news} in the prompt", "operations": []}
        assert model_json.extract_json('Use {news}: {"reply": "x", "operations": []}') == {"reply": "x", "operations": []}

    def test_the_engine_asks_again_and_never_runs_the_fragment(self):
        from backend.api.chat_engine import ChatEngine
        replies = iter(['{"operations":[{"op":"delete_node","name":"a"},{"op":"delete_node","name":"b"',
                        '{"reply":"done","operations":[{"op":"delete_node","name":"a"}]}', '{"reply":"ok"}'])
        seen, executed = [], []
        engine = ChatEngine(lambda m: seen.append(m) or next(replies), "ctx", [], "delete a")
        engine.run(lambda ops: executed.append(ops) or {"observations": []})
        assert executed == [[{"op": "delete_node", "name": "a"}]]
        assert "cut off" in seen[1][-1]["content"]

    def test_two_broken_replies_fail_without_executing(self):
        from backend.api.chat_engine import ChatEngine
        engine = ChatEngine(lambda m: '{"operations":[{"op":"delete_node","name":"a"}', "ctx", [], "delete a")
        executed = []
        with pytest.raises(ValueError, match="nothing was applied"):
            engine.run(lambda ops: executed.append(ops) or {"observations": []})
        assert executed == []

    def test_step_limit_message_is_english(self):
        from backend.api.chat_engine import ChatEngine
        engine = ChatEngine(lambda m: '{"operations":[{"op":"validate"}]}', "ctx", [], "check")
        result = engine.run(lambda ops: {"observations": ["x"]}, max_steps=2)
        assert result["reply"].startswith("Reached the step limit")


@pytest.fixture
def endpoint(monkeypatch):
    from backend.api import chat_api, graphs
    monkeypatch.setattr(graphs, "graph_exists", lambda _: True)
    monkeypatch.setattr(chat_api, "_validate", lambda g: [])
    script, calls = [], []

    def ask(messages):
        calls.append([dict(m) for m in messages])
        reply = script.pop(0)
        return reply if isinstance(reply, str) else json.dumps(reply)

    monkeypatch.setattr(chat_api, "_ask", ask)

    def chat(message, graph=None, script_=()):
        script[:] = list(script_)
        calls.clear()
        return chat_api.chat("g", {"graph": graph or canvas(), "message": message})
    return chat, calls


def test_a_claimed_edit_without_operations_is_reminded_once(endpoint):
    chat, calls = endpoint
    result = chat("delete the Risk Detector", script_=[
        "我已经删除了节点 Risk Detector。",
        {"reply": "Deleted.", "operations": [{"op": "delete_node", "name": "Risk Detector"}]}])
    assert result["applied"] and [t["name"] for t in result["graph"]["tasks"]] == ["summarize"]
    assert "nothing was changed" in calls[1][-1]["content"]
    assert "canvas_agents" not in result["graph"]


def test_an_edit_that_still_produces_nothing_says_so(endpoint):
    chat, calls = endpoint
    result = chat("删除 xyz 节点", script_=["好的，已删除。", "画布上没有 xyz。"])
    assert result["applied"] is False and len(calls) == 2
    assert "No changes were applied to the canvas." in result["notes"]


def test_a_question_is_not_reminded(endpoint):
    chat, calls = endpoint
    result = chat("how do I add a node?", script_=["Use add_node."])
    assert len(calls) == 1 and result["notes"] == []


def test_each_step_sees_the_canvas_as_it_is_now(endpoint):
    chat, calls = endpoint
    chat("rename the detector and check it", script_=[
        {"reply": "", "operations": [{"op": "rename_node", "name": "risk_detector", "new_name": "scorer"},
                                     {"op": "validate"}]},
        {"reply": "Renamed.", "operations": []}])
    observation = calls[1][-1]["content"]
    assert "Current canvas after these operations" in observation
    assert '"name": "scorer"' in observation and '"name": "risk_detector"' not in observation.split("Current canvas")[1]


def test_agent_actions_reach_the_client(endpoint):
    chat, _ = endpoint
    result = chat("删除 Research Chat", script_=[
        {"reply": "已删除。", "operations": [{"op": "delete_node", "name": "research chat"}]}])
    assert result["agent_actions"] == [{"action": "remove", "id": AGENT_ID, "name": "Research Chat",
                                     "summary": "removed Chat Agent 'Research Chat'"}]
    assert result["applied"] is False and "No changes were applied to the canvas." not in result["notes"]


def test_replay_applies_resolved_edits_to_the_current_canvas(monkeypatch):
    from backend.api import chat_api, graphs
    monkeypatch.setattr(graphs, "graph_exists", lambda _: True)
    monkeypatch.setattr(chat_api, "_validate", lambda g: [])
    current = canvas()
    current["tasks"].append(make_task("added_by_user"))
    result = chat_api.replay("g", {"graph": current, "operations": [
        {"op": "delete_node", "name": "risk_detector"}, {"op": "save_graph"}, {"op": "delete_agent", "name": "x"}]})
    assert result["applied"] and [t["name"] for t in result["graph"]["tasks"]] == ["summarize", "added_by_user"]
    assert "canvas_agents" not in result["graph"]
    gone = chat_api.replay("g", {"graph": result["graph"], "operations": [{"op": "delete_node", "name": "risk_detector"}]})
    assert gone["applied"] is False and gone["notes"]


def test_a_request_with_a_bare_verb_still_counts_as_an_edit(endpoint):
    """"加一个 chat agent" and "接到 memory 上" have no 添加/增加/连接; the
    reply "已添加" then stood with nothing behind it."""
    chat, calls = endpoint
    result = chat("加一个 chat agent", script_=[
        "已添加一个 Chat Agent。",
        {"reply": "已添加。", "operations": [{"op": "create_agent", "name": "Chat Agent"}]}])
    assert [a["action"] for a in result["agent_actions"]] == ["create"]
    assert "nothing was changed" in calls[1][-1]["content"]


def test_a_claim_after_a_read_step_is_reminded(endpoint):
    """The request read as a question to the edit check, the model looked
    first, then claimed the change; only the claim gives it away."""
    chat, calls = endpoint
    result = chat("could you get me a research agent?", script_=[
        {"reply": "", "operations": [{"op": "list_agents"}]},
        "I have added a Research agent.",
        {"reply": "Added.", "operations": [{"op": "create_agent", "name": "Research"}]}])
    assert [a["settings"]["name"] for a in result["agent_actions"]] == ["Research"]
    assert "nothing was changed" in calls[2][-1]["content"]


def test_a_claim_that_stays_unbacked_says_nothing_was_applied(endpoint):
    chat, _ = endpoint
    result = chat("could you get me a research agent?", script_=[
        "The Research agent has been created.", "It has been created."])
    assert result["agent_actions"] == []
    assert "No changes were applied to the canvas." in result["notes"]


@pytest.mark.parametrize("operation", [
    {"op": "add_chat_agent", "name": "Research"},
    {"op": "add_agent", "name": "Research"},
    {"op": "add_node", "task": {"name": "Research", "kind": "agent", "prompt": "Dig."}},
])
def test_names_a_model_reaches_for_create_the_agent(endpoint, operation):
    chat, _ = endpoint
    result = chat("加一个 research agent", script_=[{"reply": "已添加。", "operations": [operation]}])
    (action,) = result["agent_actions"]
    assert action["action"] == "create" and action["settings"]["name"] == "Research"
    assert not any(t.get("name") == "Research" for t in result["graph"]["tasks"])


def test_an_unknown_operation_is_answered_with_the_valid_ones(endpoint):
    chat, calls = endpoint
    result = chat("加一个 research agent", script_=[
        {"reply": "", "operations": [{"op": "spawn_helper", "name": "Research"}]},
        {"reply": "已添加。", "operations": [{"op": "create_agent", "name": "Research"}]}])
    observation = calls[1][-1]["content"]
    assert "Unknown operation 'spawn_helper'" in observation and "create_agent" in observation
    assert [a["action"] for a in result["agent_actions"]] == ["create"]
