"""Provider-neutral results. Callers read these, never a provider's response."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class LLMUsage:
    """Token counts as the provider reported them."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def uncached_input_tokens(self) -> int:
        return max(self.input_tokens - self.cache_read_tokens, 0)

    def as_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "uncached_input_tokens": self.uncached_input_tokens,
        }


@dataclass
class LLMResult:
    """One completion: its text, what it cost in tokens, and where it came from."""

    content: str
    usage: LLMUsage | None = None
    provider: str | None = None
    model: str | None = None
    raw: Any = field(default=None, repr=False)

    def as_dict(self) -> dict:
        return {
            "content": self.content,
            "usage": self.usage.as_dict() if self.usage else None,
            "provider": self.provider,
            "model": self.model,
        }


def normalize_usage(reported) -> LLMUsage | None:
    """A provider's usage payload as LLMUsage, or None when it reported none.

    Never invents zeros: a caller cannot tell "no tokens" from "not reported"
    if a missing payload becomes an empty count.
    """
    if reported is None:
        return None
    if not isinstance(reported, dict):
        for attribute in ("model_dump", "dict"):
            method = getattr(reported, attribute, None)
            if callable(method):
                try:
                    reported = method()
                    break
                except Exception:
                    continue
        else:
            reported = getattr(reported, "__dict__", None)
        if not isinstance(reported, dict):
            return None

    def count(*names) -> int | None:
        for name in names:
            value = reported.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if value >= 0:
                return int(value)
        return None

    def nested(section, *names) -> int:
        block = reported.get(section)
        if not isinstance(block, dict):
            return 0
        for name in names:
            value = block.get(name)
            if not isinstance(value, bool) and isinstance(value, (int, float)) and value >= 0:
                return int(value)
        return 0

    input_tokens = count("input_tokens", "prompt_tokens")
    output_tokens = count("output_tokens", "completion_tokens")
    if input_tokens is None or output_tokens is None:
        return None
    total = count("total_tokens")
    cache_read = nested("input_token_details", "cache_read", "cached_tokens")
    if not cache_read:
        cache_read = nested("prompt_tokens_details", "cached_tokens")
    reasoning = nested("output_token_details", "reasoning", "reasoning_tokens")
    if not reasoning:
        reasoning = nested("completion_tokens_details", "reasoning_tokens")
    return LLMUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total if total is not None and total >= input_tokens + output_tokens
        else input_tokens + output_tokens,
        cache_read_tokens=min(cache_read, input_tokens),
        reasoning_tokens=reasoning,
    )
