"""Coalesced batching goes through `llm.batch_result` and nothing else.

Every test patches the package's call, asserts the provider used is the one
the bridge reports (Studio never names one) and reads `LLMResult.content` /
`.usage` — no provider or framework response shape appears here.
"""
import pytest
from backend.features.execution import provider_batch


def _results(inputs, content=None):
    from llm import LLMResult, LLMUsage
    return [LLMResult(content=content if content is not None else str(value),
                      usage=LLMUsage(input_tokens=2, output_tokens=1, total_tokens=3))
            for value in inputs]


def provider():
    from backend.features import model_bridge
    return model_bridge.provider_name()


@pytest.mark.parametrize('value', [0, -1, True, 1.5, 'bad', 1025])
def test_invalid_sizes(value):
    with pytest.raises(ValueError): provider_batch.batch_size(value)


def test_factory_contract_and_standard_compatibility(monkeypatch):
    from backend.api import runner
    import llm
    monkeypatch.setattr(runner, '_make_llm', lambda **kw: 'legacy')
    assert runner._model_for_run({}) == 'legacy'
    monkeypatch.setattr(llm, 'batch_result', lambda provider, inputs: _results(inputs), raising=False)
    assert runner._model_for_run({'llm_batch_size':16, 'batch_id':'group-1'}) == 'legacy'
    monkeypatch.delattr(llm, 'batch_result')
    with pytest.raises(ValueError, match='not connected'):
        runner._model_for_run({'llm_batch_size':16, 'batch_id':'group-1'})


def test_native_batch_persists_and_forwards_options(monkeypatch):
    from backend.api import batch, runner
    import llm
    monkeypatch.setattr(llm, 'batch_result', lambda provider, inputs: _results(inputs), raising=False)
    calls = []
    def run(graph, record, **kwargs):
        calls.append(kwargs)
    monkeypatch.setattr(runner, 'start_run', run)
    monkeypatch.setattr(runner, 'get_run', lambda id: {'status':'success', 'result':{}})
    id = batch.start_batch({'id':'native-test','tasks':[], '_llm_batch_size':3}, [{'v':i} for i in range(5)], {}, workers=40)
    assert batch.wait_for(id, timeout=5)
    state = batch.get_batch(id)
    assert state['llm_batch_size'] == 3 and state['workers'] == 3
    assert len(calls) == 5
    assert all(c['llm_batch_size'] == 3 and c['batch_id'] == id for c in calls)


def test_api_rejects_unready_adapter_before_start(monkeypatch):
    import llm
    monkeypatch.delattr(llm, 'batch_result', raising=False)
    with pytest.raises(ValueError, match='not connected'):
        provider_batch.validate({}, 2)


def test_real_coalescing_tail_and_order(monkeypatch):
    import llm
    calls = []
    def batch_result(name, inputs):
        calls.append((name, inputs))
        return _results(inputs)
    monkeypatch.setattr(llm, 'batch_result', batch_result, raising=False)
    state = {'batch_id':'order', 'llm_batch_size':2}
    futures = [provider_batch.submit(i, state) for i in range(5)]
    assert [f.result(timeout=2).content for f in futures] == ['0','1','2','3','4']
    assert sorted(len(inputs) for _, inputs in calls) == [1,2,2]
    assert all(name == provider() for name, _ in calls)


def test_cancelled_pending_request_is_not_sent(monkeypatch):
    import llm
    calls = []
    monkeypatch.setattr(llm, 'batch_result',
                        lambda name, inputs: calls.append(inputs) or _results(inputs), raising=False)
    state = {'batch_id':'cancel', 'llm_batch_size':10}
    cancelled = provider_batch.submit('cancel', state)
    assert cancelled.cancel()
    survivor = provider_batch.submit('keep', state)
    assert survivor.result(timeout=2).content == 'keep'
    assert calls == [['keep']]


@pytest.mark.parametrize('bad_result', [[], RuntimeError('provider failed')])
def test_batch_failure_reaches_all_waiters(monkeypatch, bad_result):
    import llm
    def batch_result(name, inputs):
        if isinstance(bad_result, Exception): raise bad_result
        return bad_result
    monkeypatch.setattr(llm, 'batch_result', batch_result, raising=False)
    state = {'batch_id':'fail', 'llm_batch_size':2}
    futures = [provider_batch.submit(i, state) for i in range(2)]
    for future in futures:
        with pytest.raises((ValueError, RuntimeError)): future.result(timeout=2)


def test_framework_transport_and_parser(monkeypatch):
    """The framework keeps formatting and parsing; only its transport is ours."""
    import asyncio
    import llm
    from backend.features import model_bridge
    calls = []
    def batch_result(name, inputs):
        calls.append(inputs)
        return _results(inputs, content='hello')
    monkeypatch.setattr(llm, 'batch_result', batch_result, raising=False)
    state = {'batch_id':'framework','llm_batch_size':2}
    model = model_bridge.workflow_model()
    provider_batch.attach_workflow_model(model, state)
    result = asyncio.run(model.async_generate(prompt='question', system_message='system', parse_mode='str'))
    assert result is not None
    assert calls[0][0][0]['role'] == 'system'
    assert calls[0][0][-1]['content'] == 'question'
    assert state['token_usage']['total_tokens'] == 3


def test_deep_agent_tools_and_response_preserved(monkeypatch):
    import llm
    from langchain_core.messages import HumanMessage
    calls = []
    def batch_result(name, inputs, **kwargs):
        calls.append((inputs, kwargs))
        return _results(inputs, content='looked up')
    monkeypatch.setattr(llm, 'batch_result', batch_result, raising=False)
    state = {'batch_id':'deep','llm_batch_size':2}
    model = provider_batch.agent_model(state)
    tool = {'type':'function', 'function':{'name':'lookup','description':'Lookup','parameters':{'type':'object','properties':{}}}}
    result = model.bind_tools([tool]).invoke([HumanMessage(content='lookup')])
    assert result.content == 'looked up'
    assert result.usage_metadata['total_tokens'] == 3
    assert state['token_usage']['total_tokens'] == 3
    assert calls[0][1]['tools'][0]['function']['name'] == 'lookup'
    # LangChain messages reach the package as role/content dicts.
    assert calls[0][0] == [[{'role':'user','content':'lookup'}]]


def test_distinct_options_and_batches_never_mix(monkeypatch):
    import llm
    calls = []
    def batch_result(name, inputs, **kwargs):
        calls.append((inputs, kwargs))
        return _results(inputs)
    monkeypatch.setattr(llm, 'batch_result', batch_result, raising=False)
    a = {'batch_id':'a','llm_batch_size':5}
    b = {'batch_id':'b','llm_batch_size':5}
    futures = [provider_batch.submit('a', a), provider_batch.submit('b', b),
               provider_batch.submit('tool', a, temperature=0.2)]
    assert [f.result(timeout=2).content for f in futures] == ['a','b','tool']
    assert len(calls) == 3


def test_only_the_package_s_result_is_read():
    from llm import LLMResult, LLMUsage
    result = LLMResult(content='answer', usage=LLMUsage(4, 2, 6))
    assert provider_batch._text(result) == 'answer'
    assert provider_batch._usage(result)['total_tokens'] == 6
    assert provider_batch._text('plain') == 'plain'
    assert provider_batch._usage('plain') is None
    with pytest.raises(ValueError, match='LLMResult'):
        provider_batch._text(object())


def test_workflow_runner_uses_batch_after_agent_reconstruction(monkeypatch):
    import llm
    from backend.api import runner
    from conftest import make_task, make_graph
    calls = []
    def batch_result(name, inputs):
        calls.append((name, inputs))
        return _results(inputs, content='<answer>done</answer>')
    monkeypatch.setattr(llm, 'batch_result', batch_result, raising=False)
    graph = make_graph([make_task('answer', inputs=['question'], outputs=['answer'])])
    run_id = runner.start_run(graph, {'question':'hello'}, background=False,
                              batch_id='reconstructed', llm_batch_size=4)
    result = runner.get_run(run_id)
    assert result['status'] == 'success', result.get('error')
    assert calls and all(name == provider() for name, _ in calls)
    # Every coalesced call is counted into the run that made it.
    assert result['token_usage']['total_tokens'] == 3 * len(calls)


def test_native_tool_capability_is_checked_before_creating_runs(monkeypatch):
    import llm
    monkeypatch.setattr(llm, 'batch_result', lambda name, inputs: _results(inputs), raising=False)
    assert provider_batch.validate({'tasks':[]}, 4) == 4
    with pytest.raises(ValueError, match='workflow uses tools'):
        provider_batch.validate({'tasks':[{'harness':{'engine':'deepagents'}}]}, 4)
    # A package that does take tool schemas may run them.
    monkeypatch.setattr(llm, 'batch_result',
                        lambda name, inputs, tools=None: _results(inputs), raising=False)
    assert provider_batch.validate({'tasks':[{'tool_names':['x']}]}, 4) == 4
