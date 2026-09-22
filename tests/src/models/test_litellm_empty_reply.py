"""An empty reply is asked again, not handed to the agent as an answer.

Reproduced against OpenRouter's Qwen 3.x: `finish_reason: stop`, no tool
call, content "". The agent loop treated each as a step; grounding hit its
step limit twice in one 42-record batch.
"""

from types import SimpleNamespace

import pytest

from evoagentx.models import litellm_model
from evoagentx.models.litellm_model import LiteLLM, EMPTY_REPLY_RETRIES
from evoagentx.models.model_configs import LiteLLMConfig


def _reply(content):
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    message = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")],
                           usage=usage, model="openai/x")


@pytest.fixture
def llm(monkeypatch):
    model = LiteLLM(config=LiteLLMConfig(model="openai/x", api_key="k",
                                         api_base="http://localhost:1", is_local=True,
                                         output_response=False))
    monkeypatch.setattr(model, "_update_cost", lambda response: None)
    return model


def test_a_blank_reply_is_asked_again(llm, monkeypatch):
    replies = iter(["", "\n\n", "<answer>ok</answer>"])
    calls = []

    async def fake(messages, **params):
        calls.append(params)
        return _reply(next(replies))
    monkeypatch.setattr(litellm_model, "acompletion", fake)

    import asyncio
    out = asyncio.run(llm.single_generate_async([{"role": "user", "content": "hi"}]))
    assert out == "<answer>ok</answer>"
    assert len(calls) == 3


def test_it_gives_up_after_the_retries_and_returns_what_it_got(llm, monkeypatch):
    calls = []

    async def fake(messages, **params):
        calls.append(1)
        return _reply("")
    monkeypatch.setattr(litellm_model, "acompletion", fake)

    import asyncio
    out = asyncio.run(llm.single_generate_async([{"role": "user", "content": "hi"}]))
    assert out == ""
    assert len(calls) == EMPTY_REPLY_RETRIES + 1


def test_a_real_reply_is_not_asked_twice(llm, monkeypatch):
    calls = []

    def fake(messages, **params):
        calls.append(1)
        return _reply("answer")
    monkeypatch.setattr(litellm_model, "completion", fake)

    assert llm.single_generate([{"role": "user", "content": "hi"}]) == "answer"
    assert len(calls) == 1


def test_the_sync_path_retries_too(llm, monkeypatch):
    replies = iter(["", "done"])

    def fake(messages, **params):
        return _reply(next(replies))
    monkeypatch.setattr(litellm_model, "completion", fake)

    assert llm.single_generate([{"role": "user", "content": "hi"}]) == "done"
