"""Regression tests for per-event-loop async client caching.

`Evaluator` runs each example through `asyncio.run()` on a `ThreadPoolExecutor`
worker, so every call gets a fresh event loop that is closed afterwards. An
async HTTP client's connection pool belongs to the loop that created it, so
caching one client per LLM instance handed a later loop a pool owned by a dead
one: requests then blocked forever, with sockets stuck in CLOSE_WAIT at 0% CPU
and no error ever raised. The client's own `is_closed()` does not catch this,
because the client was never actually closed.

These tests assert the client is keyed by loop, which is what makes
`num_workers > 1` safe.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor

from evoagentx.models import OpenAILLM, OpenAILLMConfig


class FakeAsyncClient:
    """Stands in for AsyncOpenAI: records the loop it was created on."""

    def __init__(self):
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = None
        self._closed = False

    def is_closed(self):
        return self._closed

    async def close(self):
        self._closed = True


class StubLLM(OpenAILLM):
    """OpenAILLM with the network client swapped out."""

    def init_model(self):
        super().init_model()
        self.created = []

    def _init_async_client(self, config):
        client = FakeAsyncClient()
        self.created.append(client)
        return client


def _make_llm():
    return StubLLM(config=OpenAILLMConfig(model="gpt-4o-mini", openai_key="fake"))


class TestPerLoopCaching:
    def test_same_loop_reuses_one_client(self):
        llm = _make_llm()

        async def get_twice():
            return llm.ensure_async_client(), llm.ensure_async_client()

        first, second = asyncio.run(get_twice())
        assert first is second
        assert len(llm.created) == 1

    def test_separate_loops_get_separate_clients(self):
        """The actual bug: the second asyncio.run() must not reuse the first
        loop's client, whose connection pool is bound to a now-closed loop."""
        llm = _make_llm()

        async def get_client():
            return llm.ensure_async_client()

        first = asyncio.run(get_client())   # loop A, closed on return
        second = asyncio.run(get_client())  # loop B

        assert first is not second, "client from a closed loop was reused"
        assert second.loop is not first.loop
        assert first.loop.is_closed()
        assert len(llm.created) == 2

    def test_thread_pool_workers_get_their_own_clients(self):
        """Mirrors how Evaluator(num_workers=N) actually drives the LLM."""
        llm = _make_llm()

        def worker(_):
            async def get_client():
                return llm.ensure_async_client()
            return asyncio.run(get_client())

        with ThreadPoolExecutor(max_workers=4) as pool:
            clients = list(pool.map(worker, range(8)))

        # Every client must belong to the loop that used it, and none may be
        # shared across two different loops.
        for client in clients:
            assert client.loop is not None
        by_loop = {}
        for client in clients:
            by_loop.setdefault(client.loop, set()).add(id(client))
        assert all(len(ids) == 1 for ids in by_loop.values())

    def test_dead_loops_are_evicted(self):
        """One loop per example would otherwise leak an entry per evaluation."""
        llm = _make_llm()

        async def get_client():
            return llm.ensure_async_client()

        for _ in range(5):
            asyncio.run(get_client())

        # Only the most recent (still-referenced) entry may remain; the closed
        # loops must have been pruned rather than accumulating.
        assert len(llm._async_client_cache()) <= 1

    def test_close_only_touches_the_current_loop(self):
        llm = _make_llm()

        async def get_client():
            return llm.ensure_async_client()

        other = asyncio.run(get_client())

        async def make_and_close():
            mine = llm.ensure_async_client()
            await llm.close_async_client()
            return mine

        mine = asyncio.run(make_and_close())
        assert mine.is_closed()
        assert not other.is_closed(), "closed a client owned by another loop"

    def test_reconnects_after_close(self):
        llm = _make_llm()

        async def close_then_reuse():
            first = llm.ensure_async_client()
            await llm.close_async_client()
            second = llm.ensure_async_client()
            return first, second

        first, second = asyncio.run(close_then_reuse())
        assert first is not second
        assert not second.is_closed()


class TestSubclassesShareTheFix:
    def test_openrouter_uses_the_base_implementation(self):
        """openrouter_model.py used to carry its own copy of the buggy cache."""
        from evoagentx.models.base_model import BaseLLM
        from evoagentx.models.openrouter_model import OpenRouterLLM

        assert OpenRouterLLM.ensure_async_client is BaseLLM.ensure_async_client
        assert OpenRouterLLM.close_async_client is BaseLLM.close_async_client

    def test_openai_uses_the_base_implementation(self):
        from evoagentx.models.base_model import BaseLLM

        assert OpenAILLM.ensure_async_client is BaseLLM.ensure_async_client
        assert OpenAILLM.close_async_client is BaseLLM.close_async_client
