"""Network and provider trouble is waited out, not reported.

A dropped connection failed the node on the spot — and the run, and the
batch record — while the outage lasted seconds. Now a call keeps trying,
backing off, for a window; only then does it give up, and it says so.
"""

import asyncio
from types import SimpleNamespace

import litellm
import pytest

from evoagentx.models import litellm_model
from evoagentx.models.litellm_model import LiteLLM, TransientLLMError, is_transient
from evoagentx.models.model_configs import LiteLLMConfig


def _reply(content="ok"):
    usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2)
    message = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")],
                           usage=usage, model="openai/x")


def _conn_error():
    return litellm.InternalServerError("OpenAIException - Connection error.", "openai", "x")


@pytest.fixture
def llm(monkeypatch):
    model = LiteLLM(config=LiteLLMConfig(model="openai/x", api_key="k",
                                         api_base="http://localhost:1", is_local=True,
                                         output_response=False))
    monkeypatch.setattr(model, "_update_cost", lambda response: None)
    return model


@pytest.fixture
def no_real_sleep(monkeypatch):
    slept = []

    async def fake_async(seconds):
        slept.append(seconds)

    monkeypatch.setattr(litellm_model, "_sleep_async", fake_async)
    monkeypatch.setattr(litellm_model, "_sleep", lambda seconds: slept.append(seconds))
    return slept


class TestWhatCountsAsTransient:
    def test_the_connection_error_openrouter_actually_produced(self):
        assert is_transient(_conn_error())

    def test_timeouts_rate_limits_and_5xx(self):
        assert is_transient(litellm.Timeout("timed out", "x", "openai"))
        assert is_transient(litellm.RateLimitError("slow down", "openai", "x"))
        assert is_transient(litellm.ServiceUnavailableError("503", "openai", "x"))
        assert is_transient(litellm.APIError(502, "bad gateway", "openai", "x"))

    def test_a_bad_key_is_not(self):
        assert not is_transient(litellm.AuthenticationError("bad key", "openai", "x"))
        assert not is_transient(litellm.BadRequestError("malformed", "openai", "x"))

    def test_a_plain_socket_error_by_its_words(self):
        assert is_transient(OSError("[Errno 8] nodename nor servname provided"))
        assert is_transient(RuntimeError("Connection reset by peer"))
        assert not is_transient(ValueError("no such field"))


class TestTheCallWaitsThroughAnOutage:
    def test_async_keeps_trying_until_the_network_is_back(self, llm, monkeypatch, no_real_sleep):
        calls = []

        async def fake(messages, **params):
            calls.append(1)
            if len(calls) < 4:
                raise _conn_error()
            return _reply("back")
        monkeypatch.setattr(litellm_model, "acompletion", fake)

        out = asyncio.run(llm.single_generate_async([{"role": "user", "content": "hi"}]))
        assert out == "back"
        assert len(calls) == 4
        assert len(no_real_sleep) == 3
        assert all(1 <= s <= 61 for s in no_real_sleep)

    def test_it_backs_off_and_caps_at_a_minute(self, llm, monkeypatch, no_real_sleep):
        calls = []

        async def fake(messages, **params):
            calls.append(1)
            if len(calls) < 10:
                raise _conn_error()
            return _reply()
        monkeypatch.setattr(litellm_model, "acompletion", fake)

        asyncio.run(llm.single_generate_async([{"role": "user", "content": "hi"}]))
        assert no_real_sleep[0] < no_real_sleep[3] < no_real_sleep[6]
        assert max(no_real_sleep) <= 61

    def test_sync_waits_too(self, llm, monkeypatch, no_real_sleep):
        calls = []

        def fake(messages, **params):
            calls.append(1)
            if len(calls) < 3:
                raise litellm.Timeout("timed out", "x", "openai")
            return _reply("late")
        monkeypatch.setattr(litellm_model, "completion", fake)

        assert llm.single_generate([{"role": "user", "content": "hi"}]) == "late"
        assert len(calls) == 3

    def test_a_non_transient_error_is_raised_at_once(self, llm, monkeypatch, no_real_sleep):
        async def fake(messages, **params):
            raise litellm.AuthenticationError("bad key", "openai", "x")
        monkeypatch.setattr(litellm_model, "acompletion", fake)

        with pytest.raises(RuntimeError, match="bad key"):
            asyncio.run(llm.single_generate_async([{"role": "user", "content": "hi"}]))
        assert no_real_sleep == []


class TestGivingUp:
    def test_after_the_window_it_says_how_long_it_waited(self, llm, monkeypatch, no_real_sleep):
        monkeypatch.setattr(litellm_model, "TRANSIENT_WAIT_SECONDS", 0.0)

        async def fake(messages, **params):
            raise _conn_error()
        monkeypatch.setattr(litellm_model, "acompletion", fake)

        with pytest.raises(TransientLLMError) as info:
            asyncio.run(llm.single_generate_async([{"role": "user", "content": "hi"}]))
        assert "Could not reach 'openai/x'" in str(info.value)
        assert "Connection error" in str(info.value)
        assert isinstance(info.value.last, litellm.InternalServerError)

    def test_a_stop_request_during_the_wait_is_honoured(self, llm, monkeypatch):
        async def fake(messages, **params):
            raise _conn_error()
        monkeypatch.setattr(litellm_model, "acompletion", fake)

        async def cancel_soon():
            task = asyncio.ensure_future(
                llm.single_generate_async([{"role": "user", "content": "hi"}]))
            await asyncio.sleep(0.05)   # inside the first backoff sleep
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        asyncio.run(cancel_soon())
