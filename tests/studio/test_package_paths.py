"""Every model call Studio's execution side makes goes through the `llm` package.

The workflow path, the coalesced batch path and Evolve's optimizer path are
each driven here with only the package's call replaced: what reaches the
package is asserted, the usage it reports is asserted to land in the run or
task that made the call, and no framework model class may be constructed
along the way.
"""

import pytest
from llm import LLMResult, LLMUsage


# What a node's prompt asks for, so the framework's parser is satisfied.
ANSWER = "<answer>ok</answer>"


def _result(content="ok", input_tokens=6, output_tokens=4):
    return LLMResult(content=content, provider="test", model="test-model",
                     usage=LLMUsage(input_tokens=input_tokens, output_tokens=output_tokens,
                                    total_tokens=input_tokens + output_tokens))


@pytest.fixture
def package(monkeypatch):
    """The package's calls, recorded. Studio must use no other transport."""
    from backend.features import model_bridge
    calls = []

    def chat_result(provider, messages, **options):
        calls.append((provider, messages))
        return _result(content=ANSWER)

    async def achat_result(provider, messages, **options):
        return chat_result(provider, messages, **options)

    def batch_result(provider, items, **options):
        return [chat_result(provider, item, **options) for item in items]

    monkeypatch.setattr(model_bridge, "chat_result", chat_result)
    monkeypatch.setattr(model_bridge, "_achat_result", achat_result)
    monkeypatch.setattr(model_bridge, "batch_result", batch_result)
    return calls


@pytest.fixture
def no_framework_models(monkeypatch):
    """Studio builds no provider-specific framework model any more."""
    import evoagentx.models as models
    for name in ("LiteLLM", "OpenAILLM", "OpenRouterLLM"):
        model_class = getattr(models, name, None)
        if model_class is not None:
            monkeypatch.setattr(model_class, "__init__",
                                lambda self, *a, _name=name, **kw: pytest.fail(
                                    f"Studio constructed the framework's {_name}"))


def test_a_run_s_calls_go_through_the_package_and_are_counted(studio_data, package,
                                                              no_framework_models):
    from conftest import make_graph, make_task
    from backend.api import runner
    from backend.features import model_bridge
    hooks = set(model_bridge._hooks)
    graph = make_graph([make_task("answer", inputs=["question"], outputs=["answer"])],
                       id="package-run")
    run_id = runner.start_run(graph, {"question": "hello"}, background=False)
    run = runner.get_run(run_id)

    assert run["status"] == "success", run.get("error")
    assert package, "the node made no call through the package"
    assert all(provider == model_bridge.provider_name() for provider, _ in package)
    assert run["token_usage"] == {"input_tokens": 6 * len(package), "output_tokens": 4 * len(package),
                                  "total_tokens": 10 * len(package), "reported_calls": len(package),
                                  "source": "provider"}
    assert run["nodes"][0]["token_usage"]["total_tokens"] == 10 * len(package)
    # The hook is not left registered behind a settled run.
    assert set(model_bridge._hooks) <= hooks


def test_the_optimizer_s_own_calls_go_through_the_package_and_are_counted(
        package, no_framework_models):
    """dspy calls the model itself, and copies it to vary temperature; both
    reach the package, both are counted, and Stop is still seen per call."""
    evolve = pytest.importorskip("backend.features.evaluation.evolve_api")
    from backend.api import runner
    from backend.features import model_bridge
    from backend.features.execution import token_usage
    state = {"task_id": "package-evolve"}
    model = runner._make_llm(usage_key=token_usage.usage_key(state))
    wrapper = evolve._MiproLMWrapper(model)

    with evolve.stoppable(state):
        assert wrapper.forward(prompt="propose") == [ANSWER]
        assert wrapper.copy(temperature=0.9).forward(prompt="propose again") == [ANSWER]
        state["stop_requested"] = True
        with pytest.raises(evolve.EvolveStopped):
            wrapper.forward(prompt="once more")

    assert len(package) == 2                      # the stopped call was never sent
    assert state["token_usage"]["total_tokens"] == 20
    assert state["token_usage"]["reported_calls"] == 2
    # dspy needs a model NAME in .model; it never sees a provider or an SDK.
    assert wrapper.model == model_bridge.provider_model()
    token_usage.release_usage(state)


def test_a_coalesced_batch_reads_only_the_package_s_results(monkeypatch, no_framework_models):
    import llm
    from backend.features import model_bridge
    from backend.features.execution import provider_batch, token_usage
    sent = []
    monkeypatch.setattr(llm, "batch_result",
                        lambda provider, items, **options: (sent.append((provider, items)),
                                                            [_result() for _ in items])[1],
                        raising=False)
    state = {"batch_id": "package-batch", "llm_batch_size": 2}
    model = provider_batch.attach_workflow_model(
        model_bridge.workflow_model(usage_key=token_usage.usage_key(state)), state)
    assert model.batch_generate([[{"role": "user", "content": "a"}],
                                 [{"role": "user", "content": "b"}]]) == ["ok", "ok"]
    assert [provider for provider, _ in sent] == [model_bridge.provider_name()]
    assert state["token_usage"]["total_tokens"] == 20
    token_usage.release_usage(state)
