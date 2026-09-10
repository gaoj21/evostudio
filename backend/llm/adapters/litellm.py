"""litellm provider adapter: all API-specific construction lives here."""
def chat(cfg: dict, messages: list, kwargs: dict) -> str:
    import litellm

    response = litellm.completion(
        model=cfg["model"],
        messages=messages,
        api_key=cfg["api_key"],
        **{**cfg.get("params", {}), **kwargs},
    )
    return response.choices[0].message.content


def create_workflow_model(cfg):
    from evoagentx.models import LiteLLM, LiteLLMConfig
    prefix = cfg['model'].split('/')[0]
    field = f'{prefix}_key'
    if field not in LiteLLMConfig.model_fields:
        field = 'api_key'
    params = dict(cfg.get('params') or {})
    return LiteLLM(config=LiteLLMConfig(model=cfg['model'], **{field: cfg['api_key']}, **params))


def create_agent_model(cfg):
    from langchain_litellm import ChatLiteLLM
    params = dict(cfg.get('params') or {})
    timeout = min(float(params.pop('timeout', 120)), 120)
    return ChatLiteLLM(model=cfg['model'], api_key=cfg['api_key'], api_base=cfg.get('base_url'),
                      request_timeout=timeout, max_retries=1, **params)
