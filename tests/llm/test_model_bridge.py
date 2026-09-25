"""The bridge is the only place Studio adapts the `llm` package to a framework.

Two consumers, two shapes, one rule: neither may name a provider, import a
provider SDK or read a provider's response. What the tests check is that both
end up calling the package — and that the token usage the package reported
arrives where the run, the batch and the digest read it.
"""

import pytest

from llm import LLMResult, LLMUsage
from backend.features import model_bridge


@pytest.fixture
def package(monkeypatch):
    """Replace the package's entry points; record what the bridge asked for."""
    calls = []

    def chat_result(provider, messages, **options):
        calls.append({"provider": provider, "messages": messages, "options": options})
        return LLMResult(content="answered", provider=provider, model="stub/model",
                         usage=LLMUsage(input_tokens=100, output_tokens=20,
                                        total_tokens=120, cache_read_tokens=80,
                                        reasoning_tokens=5))

    def batch_result(provider, items, **options):
        return [chat_result(provider, item, **options) for item in items]

    async def achat_result(provider, messages, **options):
        return chat_result(provider, messages, **options)

    monkeypatch.setattr(model_bridge, "chat_result", chat_result)
    monkeypatch.setattr(model_bridge, "batch_result", batch_result)
    monkeypatch.setattr(model_bridge, "achat_result", achat_result)
    monkeypatch.setattr(model_bridge, "get_provider",
                        lambda name=None: {"model": "stub/model"})
    monkeypatch.setattr(model_bridge, "default_provider", lambda: "stub")
    monkeypatch.delenv(model_bridge.PROVIDER_ENV, raising=False)
    return calls


# ---------------------------------------------------------------------------
# which provider, and who says so
# ---------------------------------------------------------------------------

def test_the_provider_comes_from_the_environment_or_the_package(package, monkeypatch):
    assert model_bridge.provider_name() == "stub"
    monkeypatch.setenv(model_bridge.PROVIDER_ENV, "from_env")
    assert model_bridge.provider_name() == "from_env"
    assert model_bridge.provider_name("explicit") == "explicit"


def test_an_unconfigured_provider_is_reported_rather_than_raised(monkeypatch):
    def unavailable(name=None):
        raise model_bridge.ProviderError("nothing configured")

    monkeypatch.setattr(model_bridge, "get_provider", unavailable)
    assert model_bridge.provider_model() == "unavailable"


# ---------------------------------------------------------------------------
# the harness's LangChain model
# ---------------------------------------------------------------------------

def test_the_agent_model_reports_usage_the_way_langchain_does(package):
    from langchain_core.messages import HumanMessage, SystemMessage

    model = model_bridge.agent_model()
    message = model.invoke([SystemMessage(content="be brief"),
                            HumanMessage(content="hello")])
    assert message.content == "answered"
    usage = message.usage_metadata
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 20
    assert usage["total_tokens"] == 120
    assert usage["input_token_details"]["cache_read"] == 80
    assert usage["output_token_details"]["reasoning"] == 5
    # The roles LangChain uses are translated for the package, not passed on.
    assert [m["role"] for m in package[0]["messages"]] == ["system", "user"]


def test_a_result_without_usage_carries_none_and_not_zeroes(package, monkeypatch):
    monkeypatch.setattr(model_bridge, "chat_result",
                        lambda provider, messages, **options:
                            LLMResult(content="answered", usage=None))
    from langchain_core.messages import HumanMessage

    message = model_bridge.agent_model().invoke([HumanMessage(content="hi")])
    assert message.usage_metadata is None


def test_the_agent_models_usage_reaches_the_hook_it_was_given(package):
    from langchain_core.messages import HumanMessage

    seen = []
    key = model_bridge.usage_hook(seen.append)
    try:
        model_bridge.agent_model(usage_key=key).invoke([HumanMessage(content="hi")])
    finally:
        model_bridge.release_usage_hook(key)
    assert [r.usage.input_tokens for r in seen] == [100]
    # Released: a later call reports to nobody, and does not fail for it.
    model_bridge.agent_model(usage_key=key).invoke([HumanMessage(content="hi")])
    assert len(seen) == 1


def test_a_failing_hook_never_fails_the_model_call(package):
    from langchain_core.messages import HumanMessage

    def explode(result):
        raise RuntimeError("accounting is broken")

    key = model_bridge.usage_hook(explode)
    try:
        message = model_bridge.agent_model(usage_key=key).invoke(
            [HumanMessage(content="hi")])
    finally:
        model_bridge.release_usage_hook(key)
    assert message.content == "answered"


async def test_the_agent_model_has_an_async_path(package):
    from langchain_core.messages import HumanMessage

    message = await model_bridge.agent_model().ainvoke([HumanMessage(content="hi")])
    assert message.content == "answered"


# ---------------------------------------------------------------------------
# the workflow engine's model
# ---------------------------------------------------------------------------

def test_the_workflow_model_calls_the_package_for_single_and_batch(package):
    model = model_bridge.workflow_model()
    assert model.single_generate([{"role": "user", "content": "hi"}]) == "answered"
    assert model.batch_generate([[{"role": "user", "content": "a"}],
                                 [{"role": "user", "content": "b"}]]) == \
        ["answered", "answered"]
    assert len(package) == 3


def test_only_sampling_settings_are_passed_to_a_provider(package):
    model = model_bridge.workflow_model()
    model.single_generate([{"role": "user", "content": "hi"}],
                          temperature=0.2, max_tokens=64,
                          parse_mode="json", parser=object(), top_p=None)
    assert package[0]["options"] == {"temperature": 0.2, "max_tokens": 64}


def test_a_framework_agent_built_from_the_config_alone_calls_the_package(package):
    """The config travels; the model class is looked up from it.

    This is the property that makes a subprocess run go to the same place: an
    agent is rebuilt from `llm_config` on the other side, and nothing but the
    registered class name tells it where the calls go.
    """
    from evoagentx.core.registry import MODEL_REGISTRY

    model = model_bridge.workflow_model()
    config = model.config
    assert config.llm_type == "StudioLLM"

    # Only the name and the serialized config cross the boundary.
    model_class = MODEL_REGISTRY.get_model(config.llm_type)
    config_class = MODEL_REGISTRY.get_model_config(config.llm_type)
    rebuilt = model_class(config=config_class(**config.model_dump()))
    assert rebuilt.single_generate([{"role": "user", "content": "hi"}]) == "answered"
    assert package[-1]["provider"] == model_bridge.provider_name()


def test_the_workflow_models_usage_reaches_its_hook(package):
    seen = []
    key = model_bridge.usage_hook(seen.append)
    try:
        model_bridge.workflow_model(usage_key=key).single_generate(
            [{"role": "user", "content": "hi"}])
    finally:
        model_bridge.release_usage_hook(key)
    assert [r.usage.output_tokens for r in seen] == [20]


def test_the_framework_is_not_asked_to_price_a_response(package):
    """Cost is `llm.UsageTracker`'s job, computed from prices in the

    environment. A per-response number from the framework would be a second,
    disagreeing answer.
    """
    assert model_bridge.workflow_model().get_completion_cost() == 0.0


async def test_the_workflow_model_has_an_async_path(package):
    model = model_bridge.workflow_model()
    assert await model.single_generate_async(
        [{"role": "user", "content": "hi"}]) == "answered"


def test_run_async_refuses_to_nest_inside_a_running_loop(package):
    async def nothing():
        return None

    async def inner():
        coroutine = nothing()
        try:
            with pytest.raises(RuntimeError, match="running event loop"):
                model_bridge.run_async(coroutine)
        finally:
            coroutine.close()

    import asyncio

    asyncio.run(inner())
