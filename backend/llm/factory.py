"""Framework model factory. Provider-specific code belongs in adapters/."""
from .registry import get_provider
from .adapters import capability

def get_evoagentx_llm(provider: str | None = None):
    cfg = get_provider(provider)
    return capability(cfg, "create_workflow_model")(cfg)
