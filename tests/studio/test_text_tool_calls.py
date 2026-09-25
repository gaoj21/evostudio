"""The Chat Agent's model calls tools through the llm contract, which carries
text only: tools travel as an instruction and come back as parsed calls."""

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from backend.features import model_bridge, text_tool_calls


@tool
def lookup(name: str) -> str:
    """Look a name up."""
    return name


@pytest.fixture
def replies(monkeypatch):
    """The package's chat_result, answering from a script and logging what it was sent."""
    sent, script = [], []

    def chat_result(provider, messages, **options):
        sent.append((messages, options))
        content, usage = script.pop(0)
        return SimpleNamespace(content=content, usage=usage, provider=provider, model="m")

    monkeypatch.setattr(model_bridge, "chat_result", chat_result)
    return SimpleNamespace(sent=sent, script=script)


def test_a_model_can_bind_tools_and_call_them(replies):
    replies.script.append(('{"tool_calls": [{"name": "lookup", "arguments": {"name": "ACME"}}]}', None))
    model = model_bridge.agent_model().bind_tools([lookup])

    reply = model.invoke([SystemMessage(content="Be brief."), HumanMessage(content="find ACME")])

    assert reply.tool_calls[0]["name"] == "lookup" and reply.tool_calls[0]["args"] == {"name": "ACME"}
    messages, options = replies.sent[0]
    assert "tools" not in options                          # never sent as a provider option
    assert messages[0]["role"] == "system" and messages[0]["content"].startswith("Be brief.")
    assert '"lookup"' in messages[0]["content"]


def test_tool_turns_go_back_as_plain_messages(replies):
    replies.script.append(("ACME is fine.", None))
    history = [HumanMessage(content="find ACME"),
               AIMessage(content="", tool_calls=[{"name": "lookup", "args": {"name": "ACME"}, "id": "c1"}]),
               ToolMessage(content="ACME: ok", tool_call_id="c1", name="lookup")]

    reply = model_bridge.agent_model().bind_tools([lookup]).invoke(history)

    assert reply.content == "ACME is fine." and not reply.tool_calls
    messages = replies.sent[0][0]
    assert {m["role"] for m in messages} <= {"system", "user", "assistant"}
    assert '"tool_calls"' in messages[2]["content"] and messages[2]["role"] == "assistant"
    assert messages[3]["role"] == "user" and "ACME: ok" in messages[3]["content"]


def test_usage_without_cache_or_reasoning_counts_is_accepted(replies):
    """LangChain rejects None token details: a provider that reports none
    (SafeChain) failed every Chat Agent turn."""
    usage = SimpleNamespace(input_tokens=5, output_tokens=2, total_tokens=7,
                            cache_read_tokens=None, reasoning_tokens=None)
    replies.script.append(("hi", usage))

    reply = model_bridge.agent_model().invoke([HumanMessage(content="hi")])

    assert reply.usage_metadata["total_tokens"] == 7
    assert "input_token_details" not in reply.usage_metadata


def test_only_the_tools_given_are_read_as_calls():
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {}}}]
    text, calls = text_tool_calls.parse('```json\n{"tool_calls": [{"name": "lookup", "arguments": "{\\"name\\": \\"x\\"}"}]}\n```', tools)
    assert calls[0]["args"] == {"name": "x"} and text == ""
    assert text_tool_calls.parse('{"tool_calls": [{"name": "rm", "arguments": {}}]}', tools)[1] == []
    assert text_tool_calls.parse('Plain {"a": 1} answer', tools) == ('Plain {"a": 1} answer', [])
