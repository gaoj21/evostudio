"""openai_compatible provider adapter: all API-specific construction lives here."""
import json
import urllib.request
from ..registry import ProviderError

def chat(cfg: dict, messages: list, kwargs: dict) -> str:
    base_url = (cfg.get("base_url") or "").rstrip("/")
    if not base_url:
        raise ProviderError(f"Provider '{cfg['name']}' (openai_compatible) needs base_url")
    payload = {"model": cfg["model"], "messages": messages,
               **{k: v for k, v in {**cfg.get("params", {}), **kwargs}.items()
                  if k != "timeout"}}
    timeout = {**cfg.get("params", {}), **kwargs}.get("timeout", 60)
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg['api_key']}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def create_workflow_model(cfg):
    from evoagentx.models import LiteLLM, LiteLLMConfig
    return LiteLLM(config=LiteLLMConfig(model=f"openai/{cfg['model']}", is_local=True,
        api_key=cfg['api_key'], api_base=(cfg.get('base_url') or '').rstrip('/'), **dict(cfg.get('params') or {})))


def create_agent_model(cfg):
    from langchain_openai import ChatOpenAI
    params = dict(cfg.get('params') or {})
    timeout = min(float(params.pop('timeout', 120)), 120)
    return ChatOpenAI(model=cfg['model'], api_key=cfg['api_key'], base_url=cfg.get('base_url'),
                      timeout=timeout, max_retries=1, **params)
