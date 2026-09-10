"""Tests for the OpenAI-compatible agent endpoint (backend/api/agent_api.py).

This is the one place outside software can talk to Studio, so what matters is
that the contract holds for clients that were not written against it, and that
the agent loop degrades rather than crashes: a tool that fails is an
observation the model can react to, not a 500.

The LLM is replaced by a scripted `_ask` throughout — the loop's decisions are
the thing under test, not the model's wording. Long-term memory is replaced by
a fake store: the real one loads an embedding model, which is a slow way to
prove that a session id is threaded through.

Pins the bug this endpoint shipped with: it was declared `async def`, so it ran
on the event loop, where the framework's `LongTermMemory.load()` call to
`asyncio.run()` fails. The first request looked fine (no store yet, no load);
every later one returned 500.
"""

import asyncio
import json

import pytest

@pytest.fixture
def agent(monkeypatch, tmp_path):
    """agent_api with a scripted LLM and an in-memory store."""
    from backend.api import agent_api
    scripted = []

    def script(*replies):
        scripted.clear()
        scripted.extend(replies)

    def fake_ask(messages):
        agent.last_messages = list(messages)
        if not scripted:
            return json.dumps({"reply": "done"})
        return scripted.pop(0)

    stores: dict[str, list[str]] = {}

    class FakeMemory:
        def __init__(self, key):
            self.key = key

        def add(self, messages):
            stores.setdefault(self.key, []).extend(
                str(getattr(m, "content", m)) for m in messages
            )

        def save(self):
            pass

        def search(self, query, n=4):
            hits = [t for t in stores.get(self.key, []) if query and query[:6] in t]
            return [(type("M", (), {"content": t})(), 1.0) for t in hits[:n]]

    def fake_open(session, create):
        # The real store's load() calls asyncio.run(), which raises inside a
        # running event loop. Reproduced here so a handler that drifts back to
        # `async def` fails the tests instead of only failing in production —
        # and only on the second request, once a store exists to load.
        if session in stores:
            asyncio.run(asyncio.sleep(0))
        if session not in stores and not create:
            return None
        if create:
            stores.setdefault(session, [])
        return FakeMemory(session)

    monkeypatch.setattr(agent_api, "_ask", fake_ask)
    monkeypatch.setattr(agent_api, "_open_memory", fake_open)
    monkeypatch.setattr(agent_api, "MEMORY_DIR", tmp_path / "agent-memory")
    # Hermetic auth: the endpoint reads the repo .env for its shared secret, so
    # a developer who set one would otherwise get 401s here. Marking the env as
    # already loaded stops that read; TestAccessKey sets the variable itself.
    monkeypatch.setattr(agent_api, "_env_loaded", True)
    monkeypatch.delenv("EAX_AGENT_KEY", raising=False)

    agent.module = agent_api
    agent.script = script
    agent.stores = stores
    agent.last_messages = []
    return agent


def ask(agent, text, session="s1"):
    return agent.module.run_agent([{"role": "user", "content": text}], session)


class TestAgentLoop:
    def test_a_direct_answer_ends_the_turn(self, agent):
        agent.script(json.dumps({"reply": "42"}))
        assert ask(agent, "what is it?") == "42"

    def test_a_tool_result_comes_back_as_an_observation(self, agent, studio_data):
        from backend.api import skills_api
        skills_api.save_skill({"name": "tone", "description": "d", "content": "Be brief."})
        agent.script(
            json.dumps({"tool": "load_skill", "args": {"name": "tone"}}),
            json.dumps({"reply": "I read the tone skill."}),
        )
        assert ask(agent, "how should you write?") == "I read the tone skill."
        # The instructions reached the model, not just the skill's name.
        assert any("Be brief." in m["content"] for m in agent.last_messages)

    def test_a_failing_capability_is_reported_not_raised(self, agent, studio_data):
        agent.script(
            json.dumps({"tool": "load_skill", "args": {"name": "ghost"}}),
            json.dumps({"reply": "That skill does not exist."}),
        )
        assert ask(agent, "load ghost") == "That skill does not exist."
        observation = agent.last_messages[-1]["content"]
        assert "error" in observation and "ghost" in observation

    def test_an_unknown_tool_is_an_observation_too(self, agent, studio_data):
        agent.script(
            json.dumps({"tool": "no_such_tool", "args": {}}),
            json.dumps({"reply": "I cannot do that."}),
        )
        assert ask(agent, "do the impossible") == "I cannot do that."
        assert "error" in agent.last_messages[-1]["content"]

    def test_the_step_budget_ends_an_endless_loop(self, agent, studio_data):
        # Always asks for another tool, never answers.
        agent.script(*[json.dumps({"tool": "list_skills"})] * 20)
        reply = ask(agent, "keep going")
        assert "step budget" in reply

    def test_a_reply_without_a_tool_is_treated_as_the_answer(self, agent):
        agent.script(json.dumps({"thought": "just talking"}))
        assert ask(agent, "hello")  # falls back rather than looping

    def test_prose_instead_of_json_still_answers(self, agent):
        """A model that ignores the protocol should not hang the request."""
        agent.script("I am not going to use your JSON, sorry.")
        assert "JSON" in ask(agent, "hi")


class TestMemory:
    def test_remember_then_recall_within_a_session(self, agent):
        agent.script(json.dumps({"tool": "remember",
                                 "args": {"text": "threshold is 75"}}),
                     json.dumps({"reply": "noted"}))
        assert ask(agent, "remember the threshold", "alice") == "noted"
        assert agent.stores["alice"] == ["threshold is 75"]

    def test_earlier_memories_are_offered_before_the_model_asks(self, agent):
        agent.stores["alice"] = ["threshold is 75"]
        agent.script(json.dumps({"reply": "75"}))
        ask(agent, "threshold please", "alice")
        recalled = [m for m in agent.last_messages
                    if m["role"] == "system" and "earlier conversations" in m["content"]]
        assert recalled and "threshold is 75" in recalled[0]["content"]

    def test_sessions_do_not_see_each_other(self, agent):
        agent.stores["alice"] = ["threshold is 75"]
        agent.script(json.dumps({"reply": "no idea"}))
        ask(agent, "threshold please", "bob")
        assert not any("threshold is 75" in m["content"] for m in agent.last_messages)

    def test_a_session_with_no_store_yet_is_not_an_error(self, agent):
        agent.script(json.dumps({"reply": "hello"}))
        assert ask(agent, "hi", "brand-new") == "hello"


class TestOpenAISurface:
    @pytest.fixture
    def client(self, agent, studio_data):
        from fastapi.testclient import TestClient

        from backend.api import app as studio_app
        return TestClient(studio_app.app)

    def test_models_lists_the_agent(self, client):
        body = client.get("/v1/models").json()
        assert body["object"] == "list"
        assert body["data"][0]["id"] == "evoagentx-agent"

    def test_completion_has_the_shape_clients_expect(self, client, agent):
        agent.script(json.dumps({"reply": "hello there"}))
        body = client.post("/v1/chat/completions", json={
            "model": "evoagentx-agent",
            "messages": [{"role": "user", "content": "hi"}],
        }).json()

        assert body["object"] == "chat.completion"
        assert body["id"].startswith("chatcmpl-")
        choice = body["choices"][0]
        assert choice["message"] == {"role": "assistant", "content": "hello there"}
        assert choice["finish_reason"] == "stop"
        assert set(body["usage"]) == {"prompt_tokens", "completion_tokens", "total_tokens"}

    def test_the_user_field_keys_memory(self, client, agent):
        agent.script(json.dumps({"tool": "remember", "args": {"text": "carol likes json"}}),
                     json.dumps({"reply": "noted"}))
        client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "remember that"}],
            "user": "carol",
        })
        assert agent.stores["carol"] == ["carol likes json"]

    def test_streaming_returns_sse_chunks_then_done(self, client, agent):
        agent.script(json.dumps({"reply": "streamed"}))
        response = client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        })
        assert response.headers["content-type"].startswith("text/event-stream")
        lines = [l for l in response.text.splitlines() if l.startswith("data: ")]
        first = json.loads(lines[0][len("data: "):])
        assert first["object"] == "chat.completion.chunk"
        assert first["choices"][0]["delta"]["content"] == "streamed"
        assert json.loads(lines[1][len("data: "):])["choices"][0]["finish_reason"] == "stop"
        assert lines[-1] == "data: [DONE]"

    def test_messages_are_required(self, client):
        assert client.post("/v1/chat/completions", json={"model": "x"}).status_code == 400

    def test_repeated_requests_keep_working(self, client, agent):
        """The endpoint was async once, so the second request — the first with a
        memory store to load — returned 500. Two in a row would have caught it."""
        agent.script(json.dumps({"tool": "remember", "args": {"text": "a fact"}}),
                     json.dumps({"reply": "noted"}),
                     json.dumps({"reply": "still here"}))
        payload = {"messages": [{"role": "user", "content": "a fact"}], "user": "dave"}
        assert client.post("/v1/chat/completions", json=payload).status_code == 200
        assert client.post("/v1/chat/completions", json=payload).status_code == 200


class TestAccessKey:
    @pytest.fixture
    def client(self, agent, studio_data):
        from fastapi.testclient import TestClient

        from backend.api import app as studio_app
        return TestClient(studio_app.app)

    def test_open_when_no_key_is_configured(self, client, agent, monkeypatch):
        monkeypatch.delenv("EAX_AGENT_KEY", raising=False)
        agent.script(json.dumps({"reply": "ok"}))
        assert client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "hi"}]}).status_code == 200

    def test_a_configured_key_is_required(self, client, monkeypatch):
        monkeypatch.setenv("EAX_AGENT_KEY", "secret")
        assert client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "hi"}]}).status_code == 401

    def test_the_bearer_token_clients_already_send_is_accepted(self, client, agent,
                                                               monkeypatch):
        monkeypatch.setenv("EAX_AGENT_KEY", "secret")
        agent.script(json.dumps({"reply": "ok"}))
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer secret"},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 200
