"""Provider transports. Nothing outside this folder imports a provider SDK.

An adapter module offers `complete(config, messages, options) -> LLMResult`
and, optionally, `acomplete` (awaited) and `native_batch` for a provider with
a real batch endpoint. A provider config may name its own module in
`adapter`; otherwise its `type` selects a built-in.
"""

from importlib import import_module

from ..registry import ProviderError

BUILTINS = {
    "litellm": "llm.adapters.litellm_adapter",
    "openai_compatible": "llm.adapters.openai_compatible",
}


def adapter_for(config: dict):
    module = config.get("adapter") or BUILTINS.get(config.get("type"))
    if not module:
        raise ProviderError(
            f"Provider {config.get('name')!r} has unknown type {config.get('type')!r}; "
            "set 'adapter' to a module implementing complete()"
        )
    return import_module(module)


def capability(config: dict, name: str):
    """One adapter function, or None when the adapter does not offer it."""
    function = getattr(adapter_for(config), name, None)
    return function if callable(function) else None


def require(config: dict, name: str):
    function = capability(config, name)
    if function is None:
        raise ProviderError(
            f"Provider {config.get('name')!r} does not support {name}"
        )
    return function
