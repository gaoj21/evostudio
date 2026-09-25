"""Plain HTTP transport for any OpenAI-shaped /chat/completions endpoint."""

import json
import urllib.error
import urllib.request

from ..registry import ProviderError
from ..types import LLMResult, normalize_usage


def complete(config: dict, messages: list, options: dict | None = None) -> LLMResult:
    base_url = (config.get("base_url") or "").rstrip("/")
    if not base_url:
        raise ProviderError(
            f"Provider {config['name']!r} (openai_compatible) needs base_url")
    settings = {**(config.get("params") or {}), **(options or {})}
    timeout = settings.pop("timeout", 60)
    payload = {"model": config["model"], "messages": messages, **settings}
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {config['api_key']}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8"))
    choices = body.get("choices") or []
    if not choices:
        raise ProviderError(f"Provider {config['name']!r} returned no choices")
    return LLMResult(content=(choices[0].get("message") or {}).get("content") or "",
                     usage=normalize_usage(body.get("usage")),
                     provider=config.get("name"), model=config.get("model"), raw=body)


async def acomplete(config: dict, messages: list, options: dict | None = None) -> LLMResult:
    import asyncio

    return await asyncio.to_thread(complete, config, messages, options)
