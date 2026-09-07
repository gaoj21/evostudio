"""llm layer — pluggable LLM providers for the project.

Public interface:
- chat(provider, messages, **kwargs) -> str     (client.py)
- get_evoagentx_llm(provider=None)              (factory.py)
- list_providers() / get_provider(name)         (registry.py)
"""

from .client import chat
from .factory import get_evoagentx_llm
from .registry import ProviderError, get_provider, list_providers

__all__ = ["chat", "get_evoagentx_llm", "list_providers", "get_provider", "ProviderError"]
