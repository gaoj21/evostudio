"""Aggregated token usage and what it cost.

Prices are per million tokens and come from the environment, so a deployment
can price its own contract without touching code. Cached input is charged at
a fraction of the input price.
"""

import os
import threading

from .types import LLMResult, LLMUsage

DEFAULT_INPUT_PRICE_PER_1M = 2.50
DEFAULT_OUTPUT_PRICE_PER_1M = 15.00
DEFAULT_WEB_SEARCH_PRICE_PER_1K = 10.00
DEFAULT_CACHED_INPUT_RATIO = 0.10


def _price(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class UsageTracker:
    """Add each result as it arrives; read totals whenever you like."""

    def __init__(self, input_price_per_1m=None, output_price_per_1m=None,
                 web_search_price_per_1k=None, cached_input_ratio=None):
        self._lock = threading.Lock()
        self._calls = 0
        self._totals = LLMUsage()
        self._web_search_actions = 0
        self.input_price_per_1m = (input_price_per_1m if input_price_per_1m is not None
                                   else _price("EVO_TOKEN_INPUT_PRICE_PER_1M", DEFAULT_INPUT_PRICE_PER_1M))
        self.output_price_per_1m = (output_price_per_1m if output_price_per_1m is not None
                                    else _price("EVO_TOKEN_OUTPUT_PRICE_PER_1M", DEFAULT_OUTPUT_PRICE_PER_1M))
        self.web_search_price_per_1k = (web_search_price_per_1k if web_search_price_per_1k is not None
                                        else _price("EVO_WEB_SEARCH_PRICE_PER_1K", DEFAULT_WEB_SEARCH_PRICE_PER_1K))
        self.cached_input_ratio = (cached_input_ratio if cached_input_ratio is not None
                                   else _price("EVO_TOKEN_CACHED_INPUT_RATIO", DEFAULT_CACHED_INPUT_RATIO))

    def add(self, source) -> bool:
        """Count one result or usage. False when there was nothing to count."""
        usage = source.usage if isinstance(source, LLMResult) else source
        if not isinstance(usage, LLMUsage):
            return False
        with self._lock:
            self._calls += 1
            self._totals = LLMUsage(
                input_tokens=self._totals.input_tokens + usage.input_tokens,
                output_tokens=self._totals.output_tokens + usage.output_tokens,
                total_tokens=self._totals.total_tokens + usage.total_tokens,
                cache_read_tokens=self._totals.cache_read_tokens + usage.cache_read_tokens,
                reasoning_tokens=self._totals.reasoning_tokens + usage.reasoning_tokens,
            )
        return True

    def add_web_search(self, actions: int = 1) -> None:
        with self._lock:
            self._web_search_actions += max(0, int(actions))

    @property
    def totals(self) -> LLMUsage:
        with self._lock:
            return LLMUsage(**vars(self._totals))

    def token_cost(self, usage: LLMUsage | None = None) -> float:
        usage = usage if usage is not None else self.totals
        cached = usage.cache_read_tokens * self.cached_input_ratio
        input_cost = (usage.uncached_input_tokens + cached) / 1_000_000 * self.input_price_per_1m
        output_cost = usage.output_tokens / 1_000_000 * self.output_price_per_1m
        return round(input_cost + output_cost, 6)

    def snapshot(self) -> dict:
        with self._lock:
            calls, totals = self._calls, LLMUsage(**vars(self._totals))
            searches = self._web_search_actions
        token_cost = self.token_cost(totals)
        search_cost = round(searches / 1_000 * self.web_search_price_per_1k, 6)

        def per_call(value) -> float | None:
            return round(value / calls, 2) if calls else None

        return {
            "token_calls": calls,
            "input_tokens_total": totals.input_tokens,
            "uncached_input_tokens_total": totals.uncached_input_tokens,
            "output_tokens_total": totals.output_tokens,
            "total_tokens_total": totals.total_tokens,
            "cache_read_total": totals.cache_read_tokens,
            "reasoning_tokens_total": totals.reasoning_tokens,
            "total_token_cost": token_cost,
            "input_price_per_1m": self.input_price_per_1m,
            "output_price_per_1m": self.output_price_per_1m,
            "web_search_price_per_1k": self.web_search_price_per_1k,
            "cached_input_ratio": self.cached_input_ratio,
            "web_search_actions": searches,
            "avg_input_tokens_per_call": per_call(totals.input_tokens),
            "avg_uncached_input_tokens_per_call": per_call(totals.uncached_input_tokens),
            "avg_output_tokens_per_call": per_call(totals.output_tokens),
            "avg_total_tokens_per_call": per_call(totals.total_tokens),
            "avg_cache_read_per_call": per_call(totals.cache_read_tokens),
            "avg_reasoning_tokens_per_call": per_call(totals.reasoning_tokens),
            "avg_token_cost_per_call": round(token_cost / calls, 6) if calls else None,
            "total_cost": round(token_cost + search_cost, 6),
        }
