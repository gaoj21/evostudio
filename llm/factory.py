"""Build evoagentx framework LLM instances from llm-layer provider configs."""

from .registry import get_provider


def get_evoagentx_llm(provider: str | None = None):
    """Return a framework-ready LiteLLM for the named (or default) provider.

    - "litellm" providers pass the key under the model-prefix field
      (deepseek/... -> deepseek_key), falling back to generic api_key.
    - "openai_compatible" providers go through LiteLLM's openai/ prefix with
      api_base + api_key.
    """
    from evoagentx.models import LiteLLM, LiteLLMConfig

    cfg = get_provider(provider)
    params = dict(cfg.get("params") or {})
    if cfg["type"] == "litellm":
        prefix = cfg["model"].split("/")[0]
        key_field = f"{prefix}_key"
        if key_field not in LiteLLMConfig.model_fields:
            key_field = "api_key"
        config = LiteLLMConfig(model=cfg["model"], **{key_field: cfg["api_key"]}, **params)
    elif cfg["type"] == "openai_compatible":
        config = LiteLLMConfig(
            model=f"openai/{cfg['model']}",
            api_key=cfg["api_key"],
            api_base=(cfg.get("base_url") or "").rstrip("/"),
            **params,
        )
    else:
        from .registry import ProviderError

        raise ProviderError(f"Provider '{cfg['name']}' has unknown type {cfg['type']!r}")
    return LiteLLM(config=config)
