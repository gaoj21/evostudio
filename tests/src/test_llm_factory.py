"""The llm layer builds a framework model the framework will accept.

The `openai_compatible` provider type (OpenRouter, any OpenAI-shaped host)
had never been switched on: it passed a generic api_key and the framework
refused it before a single request, insisting on `openai_key`.
"""

import pytest


@pytest.fixture
def openrouter(monkeypatch):
    # The factory binds the name at import; patch where it is looked up.
    from llm import factory
    monkeypatch.setattr(factory, "get_provider", lambda name=None: {
        "name": "openrouter", "type": "openai_compatible", "model": "qwen/qwen3.8-27b",
        "base_url": "https://openrouter.ai/api/v1/", "api_key": "sk-test",
        "params": {"timeout": 120},
    })


def test_an_openai_compatible_provider_builds_without_a_request(openrouter):
    from llm.factory import get_evoagentx_llm
    llm = get_evoagentx_llm("openrouter")
    c = llm.config
    assert c.model == "openai/qwen/qwen3.8-27b"
    assert c.api_base == "https://openrouter.ai/api/v1"       # trailing slash gone
    assert c.timeout == 120
    # The request itself must carry the host and the key: the framework only
    # forwards api_base on its "local" path, so that is the path this takes.
    # Without it the call went to api.openai.com and the key was refused there.
    assert c.is_local is True
    params = llm._apply_provider_params({})
    assert params["api_base"] == "https://openrouter.ai/api/v1"
    assert params["api_key"] == "sk-test"
