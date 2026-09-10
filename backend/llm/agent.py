"""Deep Agents / LangGraph model factory, independent of business features."""
from .registry import get_provider
from .adapters import capability

def get_agent_model(provider=None):
    cfg = get_provider(provider)
    return capability(cfg, "create_agent_model")(cfg)
