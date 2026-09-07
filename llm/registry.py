"""Provider registry for the llm layer.

Config lives in llm/providers.json (repo-relative); API keys are never stored
there — each provider names an env var (`api_key_env`) read from the repo-root
.env / process environment. Several providers can be registered at once;
`enabled: false` entries are documented examples and unusable until flipped.
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONFIG_PATH = Path(__file__).resolve().parent / "providers.json"

_cache = None


class ProviderError(Exception):
    """Unknown / disabled / misconfigured provider."""


def _load() -> dict:
    global _cache
    if _cache is None:
        load_dotenv(_REPO_ROOT / ".env")
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            _cache = json.load(f)
    return _cache


def list_providers() -> dict:
    """All configured providers, with availability (env key present)."""
    out = {}
    for name, cfg in _load().get("providers", {}).items():
        out[name] = {
            **cfg,
            "available": bool(cfg.get("enabled", True))
            and bool(os.getenv(cfg.get("api_key_env", ""))),
        }
    return out


def get_provider(name: str | None = None) -> dict:
    """Provider config by name (default from providers.json), with the API
    key resolved into cfg["api_key"]. Raises ProviderError with a clear
    message otherwise."""
    config = _load()
    name = name or config.get("default")
    cfg = (config.get("providers") or {}).get(name)
    if cfg is None:
        raise ProviderError(
            f"Unknown LLM provider '{name}'. Configured: {sorted(config.get('providers', {}))}"
        )
    if not cfg.get("enabled", True):
        raise ProviderError(f"LLM provider '{name}' is disabled in providers.json")
    api_key = os.getenv(cfg.get("api_key_env", ""))
    if not api_key:
        raise ProviderError(
            f"LLM provider '{name}' requires env var {cfg.get('api_key_env')} (not set)"
        )
    return {**cfg, "name": name, "api_key": api_key}
