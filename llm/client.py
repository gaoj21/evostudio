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


def _chat_litellm(cfg: dict, messages: list, kwargs: dict) -> str:
    import litellm

    response = litellm.completion(
        model=cfg["model"],
        messages=messages,
        api_key=cfg["api_key"],
        **{**cfg.get("params", {}), **kwargs},
    )
    return response.choices[0].message.content


def _chat_openai_compatible(cfg: dict, messages: list, kwargs: dict) -> str:
    base_url = (cfg.get("base_url") or "").rstrip("/")
    if not base_url:
        raise ProviderError(f"Provider '{cfg['name']}' (openai_compatible) needs base_url")
    payload = {"model": cfg["model"], "messages": messages,
               **{k: v for k, v in {**cfg.get("params", {}), **kwargs}.items()
                  if k != "timeout"}}
    timeout = {**cfg.get("params", {}), **kwargs}.get("timeout", 60)
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg['api_key']}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


_DISPATCH = {
    "litellm": _chat_litellm,
    "openai_compatible": _chat_openai_compatible,
}


def chat(provider: str | None, messages: list, **kwargs) -> str:
    """Send chat messages through the named (or default) provider; returns
    the assistant message content as a string."""
    cfg = get_provider(provider)
    handler = _DISPATCH.get(cfg.get("type"))
    if handler is None:
        raise ProviderError(
            f"Provider '{cfg['name']}' has unknown type {cfg.get('type')!r} "
            f"(supported: {sorted(_DISPATCH)})"
        )
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
