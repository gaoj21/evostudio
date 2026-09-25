"""The registry answers one question: may this provider be used, and with what?

Every failure mode is a ProviderError that names what is wrong, because the
caller shows that text to a person who has to fix an environment.
"""

import json

import pytest

from llm import ProviderError, default_provider, get_provider, list_providers
from llm import registry


@pytest.fixture
def providers(tmp_path, monkeypatch):
    """Point the registry at a config of our own, not the repository's."""

    def write(config: dict):
        path = tmp_path / "providers.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        monkeypatch.setattr(registry, "_CONFIG_PATH", path)
        monkeypatch.setattr(registry, "_ENV_PATHS", ())
        registry.reload()

    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    yield write
    registry.reload()


BASIC = {
    "default": "house",
    "providers": {
        "house": {"type": "litellm", "model": "house/small",
                  "api_key_env": "HOUSE_KEY"},
        "example": {"type": "litellm", "model": "x", "enabled": False},
        "two_secrets": {"type": "litellm", "model": "y",
                        "api_key_env": "HOUSE_KEY",
                        "requires": ["HOUSE_REGION"]},
    },
}


def test_an_unknown_provider_names_the_ones_that_exist(providers):
    providers(BASIC)
    with pytest.raises(ProviderError) as error:
        get_provider("nope")
    assert "nope" in str(error.value)
    assert "house" in str(error.value)       # the caller can see the choices


def test_a_disabled_provider_is_refused_rather_than_tried(providers, monkeypatch):
    providers(BASIC)
    monkeypatch.setenv("HOUSE_KEY", "sk-test")
    with pytest.raises(ProviderError, match="disabled"):
        get_provider("example")


def test_a_missing_variable_is_named(providers, monkeypatch):
    providers(BASIC)
    monkeypatch.delenv("HOUSE_KEY", raising=False)
    with pytest.raises(ProviderError, match="HOUSE_KEY"):
        get_provider("house")


def test_every_required_variable_is_checked_and_resolved(providers, monkeypatch):
    providers(BASIC)
    monkeypatch.setenv("HOUSE_KEY", "sk-test")
    monkeypatch.delenv("HOUSE_REGION", raising=False)
    with pytest.raises(ProviderError, match="HOUSE_REGION"):
        get_provider("two_secrets")
    monkeypatch.setenv("HOUSE_REGION", "eu")
    config = get_provider("two_secrets")
    assert config["api_key"] == "sk-test"
    assert config["secrets"] == {"HOUSE_KEY": "sk-test", "HOUSE_REGION": "eu"}
    assert config["name"] == "two_secrets"


def test_availability_reflects_the_environment_not_the_next_request(providers, monkeypatch):
    providers(BASIC)
    monkeypatch.delenv("HOUSE_KEY", raising=False)
    monkeypatch.delenv("HOUSE_REGION", raising=False)
    listed = list_providers()
    assert listed["house"]["available"] is False
    assert listed["house"]["missing_env"] == ["HOUSE_KEY"]
    assert listed["house"]["default"] is True
    # A disabled provider is never available, whatever the environment holds.
    assert listed["example"]["available"] is False
    monkeypatch.setenv("HOUSE_KEY", "sk-test")
    assert list_providers()["house"]["available"] is True
    assert list_providers()["example"]["available"] is False


def test_the_default_comes_from_the_environment_before_the_file(providers, monkeypatch):
    providers(BASIC)
    assert default_provider() == "house"
    monkeypatch.setenv("LLM_PROVIDER", "two_secrets")
    assert default_provider() == "two_secrets"
    # ...and a caller naming none gets that one.
    monkeypatch.setenv("HOUSE_KEY", "sk-test")
    monkeypatch.setenv("HOUSE_REGION", "eu")
    assert get_provider(None)["name"] == "two_secrets"


def test_the_repositorys_own_config_is_loadable_and_has_a_default():
    """Whatever this checkout ships must at least parse and name a default."""
    registry.reload()
    assert default_provider() in list_providers()
