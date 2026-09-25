"""Standalone LLM API — the single boundary for model calls (llm-v12).

Business code needs to know only which provider to use, what messages to
send, whether it wants text or an LLMResult, and whether it wants usage
statistics. Nothing outside this package should import a provider SDK or read
a provider's response shape.

    from llm import chat, chat_result, batch_result, UsageTracker

See README.md for the contract.
"""

from .client import (
    abatch,
    abatch_result,
    achat,
    achat_result,
    batch,
    batch_result,
    chat,
    chat_result,
)
from .registry import ProviderError, default_provider, get_provider, list_providers
from .types import LLMResult, LLMUsage
from .usage import UsageTracker

__all__ = [
    "chat",
    "chat_result",
    "batch",
    "batch_result",
    "abatch",
    "abatch_result",
    "achat",
    "achat_result",
    "LLMResult",
    "LLMUsage",
    "UsageTracker",
    "list_providers",
    "get_provider",
    "default_provider",
    "ProviderError",
]
