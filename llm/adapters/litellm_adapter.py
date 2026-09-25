"""litellm transport: every litellm detail lives here."""

from ..types import LLMResult, normalize_usage


def _options(config: dict, options: dict) -> dict:
    return {**(config.get("params") or {}), **(options or {})}


def _result(config: dict, response) -> LLMResult:
    content = response.choices[0].message.content or ""
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    return LLMResult(content=content, usage=normalize_usage(usage),
                     provider=config.get("name"), model=config.get("model"), raw=response)


def complete(config: dict, messages: list, options: dict | None = None) -> LLMResult:
    import litellm

    return _result(config, litellm.completion(
        model=config["model"], messages=messages, api_key=config["api_key"],
        **_options(config, options)))


async def acomplete(config: dict, messages: list, options: dict | None = None) -> LLMResult:
    import litellm

    return _result(config, await litellm.acompletion(
        model=config["model"], messages=messages, api_key=config["api_key"],
        **_options(config, options)))
