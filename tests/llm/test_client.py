"""The calls business code makes, with the transport replaced by a stub.

No test here touches a network: a provider config names an adapter module we
build in-process, which is the same extension point a real vendor uses.
"""

import asyncio
import sys
from types import ModuleType

import pytest

from llm import (
    ProviderError,
    abatch,
    abatch_result,
    achat,
    achat_result,
    batch,
    batch_result,
    chat,
    chat_result,
)
from llm import client
from llm.types import LLMResult, LLMUsage


class Transport:
    """A recorded, scriptable stand-in for a provider's transport."""

    def __init__(self, *, answers=None, errors=None, usage=None):
        self.answers = list(answers or [])
        self.errors = list(errors or [])
        self.usage = usage
        self.calls = []

    def complete(self, config, messages, options=None):
        self.calls.append((messages, dict(options or {})))
        if self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error
        content = self.answers.pop(0) if self.answers else "ok"
        return LLMResult(content=content, usage=self.usage,
                         provider=config.get("name"), model=config.get("model"))

    async def acomplete(self, config, messages, options=None):
        return self.complete(config, messages, options)


@pytest.fixture
def provider(monkeypatch, request):
    """Install an adapter module and make every provider lookup return it."""
    installed = []

    def install(transport, *, name="stub", acomplete=True,
                native_batch=None, anative_batch=None, params=None):
        module = ModuleType(f"llm_test_adapter_{len(installed)}")
        module.complete = transport.complete
        if acomplete:
            module.acomplete = transport.acomplete
        if native_batch is not None:
            module.native_batch = native_batch
        if anative_batch is not None:
            module.anative_batch = anative_batch
        monkeypatch.setitem(sys.modules, module.__name__, module)
        installed.append(module)
        config = {"name": name, "type": "stub", "adapter": module.__name__,
                  "model": "stub/model", "api_key": "sk-test",
                  "params": params or {}}
        monkeypatch.setattr(client, "get_provider", lambda requested=None: config)
        return config

    # Retries must not make the suite wait out real backoffs.
    monkeypatch.setattr(client, "RETRY_BACKOFF", (0, 0))
    monkeypatch.setattr(client.time, "sleep", lambda seconds: None)

    async def no_sleep(seconds):
        return None

    monkeypatch.setattr(client.asyncio, "sleep", no_sleep)
    return install


MESSAGES = [{"role": "user", "content": "hello"}]


# ---------------------------------------------------------------------------
# the two shapes of a call
# ---------------------------------------------------------------------------

def test_chat_returns_text_and_chat_result_returns_the_usage(provider):
    provider(Transport(answers=["hi", "hi"], usage=LLMUsage(9, 1, 10)))
    assert chat(None, MESSAGES) == "hi"
    result = chat_result(None, MESSAGES)
    assert isinstance(result, LLMResult)
    assert result.content == "hi"
    assert result.usage.input_tokens == 9
    assert (result.provider, result.model) == ("stub", "stub/model")


def test_provider_params_are_the_floor_and_options_override_them(provider):
    transport = Transport()
    provider(transport, params={"timeout": 120, "temperature": 0.9})
    chat(None, MESSAGES, temperature=0.0)
    messages, options = transport.calls[0]
    assert messages is not None
    # The adapter receives the caller's options; merging with the provider's
    # own params is the adapter's job, so what arrives here is the request.
    assert options == {"temperature": 0.0}


def test_an_empty_message_list_is_refused_before_any_request(provider):
    transport = Transport()
    provider(transport)
    for bad in ([], None, "hello"):
        with pytest.raises(ProviderError):
            chat(None, bad)
    assert transport.calls == []


def test_a_provider_without_the_capability_says_so(monkeypatch):
    module = ModuleType("llm_test_adapter_empty")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(client, "get_provider", lambda requested=None: {
        "name": "bare", "adapter": module.__name__, "model": "m", "api_key": "k"})
    with pytest.raises(ProviderError, match="complete"):
        chat(None, MESSAGES)


# ---------------------------------------------------------------------------
# retries
# ---------------------------------------------------------------------------

class Status(Exception):
    def __init__(self, status_code):
        super().__init__(f"http {status_code}")
        self.status_code = status_code


def test_a_rate_limit_is_retried_and_then_succeeds(provider):
    transport = Transport(answers=["late"], errors=[Status(429)])
    provider(transport)
    assert chat(None, MESSAGES) == "late"
    assert len(transport.calls) == 2


def test_a_server_error_is_retried(provider):
    transport = Transport(answers=["late"], errors=[Status(503)])
    provider(transport)
    assert chat(None, MESSAGES) == "late"
    assert len(transport.calls) == 2


def test_a_bad_request_is_raised_at_once_rather_than_waiting_out_backoffs(provider):
    transport = Transport(errors=[Status(400)])
    provider(transport)
    with pytest.raises(Status):
        chat(None, MESSAGES)
    assert len(transport.calls) == 1


def test_a_provider_error_is_never_retried(provider):
    transport = Transport(errors=[ProviderError("misconfigured")])
    provider(transport)
    with pytest.raises(ProviderError, match="misconfigured"):
        chat(None, MESSAGES)
    assert len(transport.calls) == 1


def test_exhausted_retries_report_the_last_error(provider):
    transport = Transport(errors=[Status(429), Status(429), Status(429)])
    provider(transport)
    with pytest.raises(ProviderError) as error:
        chat(None, MESSAGES)
    assert "after retries" in str(error.value)
    assert len(transport.calls) == 3       # the first try plus one per backoff


# ---------------------------------------------------------------------------
# batches
# ---------------------------------------------------------------------------

ITEMS = [[{"role": "user", "content": "one"}],
         [{"role": "user", "content": "two"}],
         [{"role": "user", "content": "three"}]]


def test_a_parallel_batch_keeps_the_order_it_was_given(provider):
    # Answers are returned in completion order by the stub, so the only way
    # the results can line up is if the client restores the caller's order.
    class Ordered(Transport):
        def complete(self, config, messages, options=None):
            self.calls.append((messages, dict(options or {})))
            return LLMResult(content=messages[0]["content"].upper())

    provider(Ordered())
    assert batch(None, ITEMS) == ["ONE", "TWO", "THREE"]
    results = batch_result(None, ITEMS)
    assert [r.content for r in results] == ["ONE", "TWO", "THREE"]


def test_a_native_batch_is_used_when_the_provider_has_one(provider):
    seen = {}

    def native_batch(config, items, options):
        seen["items"] = items
        return [LLMResult(content=item[0]["content"]) for item in items]

    transport = Transport()
    provider(transport, native_batch=native_batch)
    assert batch(None, ITEMS) == ["one", "two", "three"]
    assert len(seen["items"]) == 3
    assert transport.calls == []          # nothing went out one at a time


def test_a_native_batch_returning_the_wrong_number_of_results_is_refused(provider):
    def short(config, items, options):
        return [LLMResult(content="only one")]

    provider(Transport(), native_batch=short)
    with pytest.raises(ProviderError) as error:
        batch_result(None, ITEMS)
    assert "1 results for 3 requests" in str(error.value)

    def not_a_list(config, items, options):
        return "answers"

    provider(Transport(), native_batch=not_a_list)
    with pytest.raises(ProviderError):
        batch_result(None, ITEMS)


def test_an_empty_batch_is_refused(provider):
    provider(Transport())
    with pytest.raises(ProviderError):
        batch(None, [])
    with pytest.raises(ProviderError):
        batch(None, [[]])


# ---------------------------------------------------------------------------
# the async paths
# ---------------------------------------------------------------------------

async def test_achat_uses_the_adapters_async_transport(provider):
    provider(Transport(answers=["async"], usage=LLMUsage(2, 1, 3)))
    assert await achat(None, MESSAGES) == "async"
    assert (await achat_result(None, MESSAGES)).usage.output_tokens == 1


async def test_an_adapter_without_an_async_transport_still_works(provider):
    transport = Transport(answers=["threaded"])
    provider(transport, acomplete=False)
    assert await achat(None, MESSAGES) == "threaded"
    assert len(transport.calls) == 1


async def test_an_async_rate_limit_is_retried(provider):
    transport = Transport(answers=["late"], errors=[Status(429)])
    provider(transport)
    assert await achat(None, MESSAGES) == "late"
    assert len(transport.calls) == 2


async def test_an_async_batch_keeps_its_order_and_bounds_its_concurrency(provider):
    live = {"now": 0, "peak": 0}

    class Counting(Transport):
        async def acomplete(self, config, messages, options=None):
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
            await asyncio.sleep(0)
            live["now"] -= 1
            return LLMResult(content=messages[0]["content"].upper())

    provider(Counting())
    items = ITEMS * 10
    assert await abatch(None, items) == [i[0]["content"].upper() for i in items]
    assert live["peak"] <= client.MAX_PARALLEL


async def test_an_async_native_batch_is_awaited(provider):
    async def anative_batch(config, items, options):
        return [LLMResult(content="native") for _ in items]

    provider(Transport(), anative_batch=anative_batch)
    assert await abatch_result(None, ITEMS) == [
        LLMResult(content="native") for _ in ITEMS]
