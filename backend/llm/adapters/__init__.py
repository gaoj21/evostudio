"""Provider plugins. Business modules must not import SDKs directly."""
from importlib import import_module
from ..registry import ProviderError

BUILTINS = {"litellm": "llm.adapters.litellm", "openai_compatible": "llm.adapters.openai_compatible"}

def get_adapter(config):
    module = config.get("adapter") or BUILTINS.get(config.get("type"))
    if not module:
        raise ProviderError(f"Unknown provider type {config.get('type')!r}; configure an adapter module")
    return import_module(module)

def capability(config, name):
    adapter = get_adapter(config)
    function = getattr(adapter, name, None)
    if not callable(function):
        raise ProviderError(f"Adapter {adapter.__name__} does not support {name}")
    return function
