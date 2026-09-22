"""llm layer — pluggable LLM providers for the project.

Public interface:
- chat(provider, messages, **kwargs) -> str     (client.py)
- get_evoagentx_llm(provider=None)              (factory.py)
- list_providers() / get_provider(name)         (registry.py)
"""

from .agent import get_agent_model
from .client import chat
from .factory import get_evoagentx_llm
from .registry import ProviderError, get_provider, list_providers

__all__ = ["get_agent_model", "chat", "get_evoagentx_llm", "list_providers", "get_provider", "ProviderError"]
