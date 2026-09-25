"""Provider registry.

Config lives in llm/providers.json next to this module; secrets never do.
Each provider names the environment variables it needs (`api_key_env`, or
`requires` for several), read from the process environment and the
repository's .env. `enabled: false` entries are documented examples.
"""

import json
import os
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent
_CONFIG_PATH = _PACKAGE_ROOT / "providers.json"
# The package may sit inside a checkout or beside one; load both .env spots.
_ENV_PATHS = (_PACKAGE_ROOT.parent / ".env", _PACKAGE_ROOT / ".env")

_cache = None
_env_loaded = False


class ProviderError(Exception):
    """Unknown, disabled or misconfigured provider; also an unsupported call."""


def _load_env() -> None:
    global _env_loaded
    if _env_loaded:
        return
    _env_loaded = True
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for path in _ENV_PATHS:
        if path.is_file():
            load_dotenv(path)


def _config() -> dict:
    global _cache
    if _cache is None:
        _load_env()
        with open(_CONFIG_PATH, encoding="utf-8") as stream:
            _cache = json.load(stream)
    return _cache


def reload() -> None:
    """Forget the cached config (tests, or after editing providers.json)."""
    global _cache, _env_loaded
    _cache = None
    _env_loaded = False


def required_env(config: dict) -> list[str]:
    names = [config["api_key_env"]] if config.get("api_key_env") else []
    names += list(config.get("requires") or [])
    return names


def default_provider() -> str | None:
    """The provider used when a caller names none."""
    return os.getenv("LLM_PROVIDER") or _config().get("default")


def list_providers() -> dict:
    """Every configured provider and whether its environment is complete.

    `available` means the variables it needs are set — not that the next
    request will succeed.
    """
    out = {}
    for name, config in (_config().get("providers") or {}).items():
        missing = [key for key in required_env(config) if not os.getenv(key)]
        out[name] = {
            **config,
            "enabled": bool(config.get("enabled", True)),
            "available": bool(config.get("enabled", True)) and not missing,
            "missing_env": missing,
            "default": name == default_provider(),
        }
    return out


def get_provider(name: str | None = None) -> dict:
    """One provider's configuration, with its secrets resolved.

    Raises ProviderError naming what is wrong: unknown provider, disabled
    provider, or a missing environment variable.
    """
    config = _config()
    name = name or default_provider()
    providers = config.get("providers") or {}
    resolved = providers.get(name)
    if resolved is None:
        raise ProviderError(
            f"Unknown LLM provider {name!r}. Configured: {sorted(providers)}"
        )
    if not resolved.get("enabled", True):
        raise ProviderError(f"LLM provider {name!r} is disabled in providers.json")
    missing = [key for key in required_env(resolved) if not os.getenv(key)]
    if missing:
        raise ProviderError(
            f"LLM provider {name!r} needs {', '.join(missing)} in the environment"
        )
    secrets = {key: os.environ[key] for key in required_env(resolved)}
    api_key = secrets.get(resolved.get("api_key_env", "")) if resolved.get("api_key_env") else None
    return {**resolved, "name": name, "api_key": api_key, "secrets": secrets}
