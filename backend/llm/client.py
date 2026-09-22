"""Unified chat interface for the llm layer.

`chat(provider, messages, **kwargs) -> str` dispatches on the provider's
`type` — different provider types genuinely call different APIs:

- "litellm": via litellm.completion (the multi-provider SDK)
- "openai_compatible": plain HTTP POST to {base_url}/chat/completions

Both paths share a 429/5xx retry (3 attempts, 5s/15s backoff) and a timeout
(provider `params.timeout`, default 60s).
"""

import json
import time
import urllib.error
import urllib.request

from .registry import ProviderError, get_provider

RETRY_BACKOFF = [5, 15]


def _retryable(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    if code is not None:
        return code == 429 or 500 <= code < 600
    # litellm wraps HTTP errors in its own exceptions; check the status attr
    status = getattr(exc, "status_code", None)
    return status == 429 or (status is not None and 500 <= status < 600)


from .adapters.litellm import chat as _chat_litellm
from .adapters.openai_compatible import chat as _chat_openai_compatible
from .adapters import capability


_DISPATCH = {
    "litellm": _chat_litellm,
    "openai_compatible": _chat_openai_compatible,
}


def chat(provider: str | None, messages: list, **kwargs) -> str:
    """Send chat messages through the named (or default) provider; returns
    the assistant message content as a string."""
    cfg = get_provider(provider)
    handler = capability(cfg, "chat") if cfg.get('adapter') else _DISPATCH.get(cfg.get("type"))
    if handler is None:
        handler = capability(cfg, "chat")
    last_error = None
    for attempt in range(len(RETRY_BACKOFF) + 1):
        if attempt:
            time.sleep(RETRY_BACKOFF[attempt - 1])
        try:
            return handler(cfg, messages, kwargs)
        except Exception as e:
            last_error = e
            if not _retryable(e):
                raise
    raise ProviderError(f"LLM call failed after retries: {last_error}")
