"""The calls business code makes: chat, batch, and their async and result forms.

Two shapes for each: the plain one returns text, the `_result` one returns an
LLMResult carrying the provider's token usage. Retries cover rate limits and
server errors; anything else is raised as it is, so a bad request does not
wait out three backoffs.
"""

import asyncio
import concurrent.futures
import time

from .adapters import capability, require
from .registry import ProviderError, get_provider
from .types import LLMResult

RETRY_BACKOFF = (5, 15)
MAX_PARALLEL = 8


def _retryable(error: Exception) -> bool:
    for attribute in ("status_code", "code", "http_status"):
        status = getattr(error, attribute, None)
        if isinstance(status, int):
            return status == 429 or 500 <= status < 600
    text = str(error).lower()
    return any(mark in text for mark in ("rate limit", "429", "timeout", "temporarily"))


def _messages(messages) -> list:
    if not isinstance(messages, list) or not messages:
        raise ProviderError("messages must be a non-empty list of {'role', 'content'} dicts")
    return messages


def _notify(options: dict, result: LLMResult) -> LLMResult:
    """Hand the result to an adapter-level observer, when one was passed."""
    callback = options.pop("_llm_usage_callback", None) if options else None
    if callable(callback):
        try:
            callback(result)
        except Exception:
            pass
    return result


def chat_result(provider: str | None, messages: list, **options) -> LLMResult:
    """One completion with its token usage."""
    config = get_provider(provider)
    call = require(config, "complete")
    settings = dict(options)
    callback = settings.pop("_llm_usage_callback", None)
    last_error = None
    for attempt in range(len(RETRY_BACKOFF) + 1):
        if attempt:
            time.sleep(RETRY_BACKOFF[attempt - 1])
        try:
            result = call(config, _messages(messages), settings)
            return _notify({"_llm_usage_callback": callback}, result)
        except ProviderError:
            raise
        except Exception as error:
            last_error = error
            if not _retryable(error):
                raise
    raise ProviderError(f"LLM call to {config['name']!r} failed after retries: {last_error}")


def chat(provider: str | None, messages: list, **options) -> str:
    """One completion's text. Use chat_result when token usage is needed."""
    return chat_result(provider, messages, **options).content


async def achat_result(provider: str | None, messages: list, **options) -> LLMResult:
    config = get_provider(provider)
    call = capability(config, "acomplete")
    if call is None:
        return await asyncio.to_thread(chat_result, provider, messages, **options)
    settings = dict(options)
    callback = settings.pop("_llm_usage_callback", None)
    last_error = None
    for attempt in range(len(RETRY_BACKOFF) + 1):
        if attempt:
            await asyncio.sleep(RETRY_BACKOFF[attempt - 1])
        try:
            result = await call(config, _messages(messages), settings)
            return _notify({"_llm_usage_callback": callback}, result)
        except ProviderError:
            raise
        except Exception as error:
            last_error = error
            if not _retryable(error):
                raise
    raise ProviderError(f"LLM call to {config['name']!r} failed after retries: {last_error}")


async def achat(provider: str | None, messages: list, **options) -> str:
    return (await achat_result(provider, messages, **options)).content


def _items(items) -> list:
    if not isinstance(items, list) or not items:
        raise ProviderError("batch items must be a non-empty list of message lists")
    return [_messages(item) for item in items]


def batch_result(provider: str | None, items: list, **options) -> list[LLMResult]:
    """One LLMResult per message list, in the order given.

    A provider with a real batch endpoint uses it; otherwise the requests go
    out in parallel, bounded, which is what a caller means by a batch.
    """
    config = get_provider(provider)
    items = _items(items)
    native = capability(config, "native_batch")
    if native is not None:
        results = native(config, items, dict(options))
        if not isinstance(results, list) or len(results) != len(items):
            raise ProviderError(
                f"Provider {config['name']!r} returned {len(results) if isinstance(results, list) else '?'} "
                f"results for {len(items)} requests")
        return results
    workers = min(MAX_PARALLEL, len(items))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda item: chat_result(provider, item, **options), items))


def batch(provider: str | None, items: list, **options) -> list[str]:
    """The text of each completion, in the order given."""
    return [result.content for result in batch_result(provider, items, **options)]


async def abatch_result(provider: str | None, items: list, **options) -> list[LLMResult]:
    config = get_provider(provider)
    items = _items(items)
    native = capability(config, "anative_batch")
    if native is not None:
        return await native(config, items, dict(options))
    semaphore = asyncio.Semaphore(MAX_PARALLEL)

    async def one(item):
        async with semaphore:
            return await achat_result(provider, item, **options)

    return list(await asyncio.gather(*(one(item) for item in items)))


async def abatch(provider: str | None, items: list, **options) -> list[str]:
    return [result.content for result in await abatch_result(provider, items, **options)]
