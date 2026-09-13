import pytest
from backend.features.execution import provider_batch


@pytest.mark.parametrize('value', [0, -1, True, 1.5, 'bad', 1025])
def test_invalid_sizes(value):
    with pytest.raises(ValueError): provider_batch.batch_size(value)


def test_factory_contract_and_standard_compatibility(monkeypatch):
    from backend.api import runner
    import llm
    monkeypatch.setattr(runner, '_make_llm', lambda: 'legacy')
    assert runner._model_for_run({}) == 'legacy'
    monkeypatch.setattr(llm, 'batch', lambda provider, inputs: inputs, raising=False)
    assert runner._model_for_run({'llm_batch_size':16, 'batch_id':'group-1'}) == 'legacy'
    monkeypatch.delattr(llm, 'batch')
    with pytest.raises(ValueError, match='not connected'):
        runner._model_for_run({'llm_batch_size':16, 'batch_id':'group-1'})


def test_native_batch_persists_and_forwards_options(monkeypatch):
    from backend.api import batch, runner
    import llm
    monkeypatch.setattr(llm, 'batch', lambda provider, inputs: inputs, raising=False)
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
    monkeypatch.delattr(llm, 'batch', raising=False)
    with pytest.raises(ValueError, match='not connected'):
        provider_batch.validate({}, 2)


def test_real_coalescing_tail_and_order(monkeypatch):
    import llm
    calls = []
    def batch(provider, inputs):
        calls.append((provider, inputs))
        return [str(value) for value in inputs]
    monkeypatch.setattr(llm, 'batch', batch, raising=False)
    state = {'batch_id':'order', 'llm_batch_size':2}
    futures = [provider_batch.submit(i, state) for i in range(5)]
    assert [f.result(timeout=2) for f in futures] == ['0','1','2','3','4']
    assert sorted(len(inputs) for _, inputs in calls) == [1,2,2]
    assert all(provider == 'safechain' for provider, _ in calls)


def test_cancelled_pending_request_is_not_sent(monkeypatch):
    import llm
    calls = []
    monkeypatch.setattr(llm, 'batch', lambda provider, inputs: calls.append(inputs) or inputs, raising=False)
    state = {'batch_id':'cancel', 'llm_batch_size':10}
    cancelled = provider_batch.submit('cancel', state)
    assert cancelled.cancel()
    survivor = provider_batch.submit('keep', state)
    assert survivor.result(timeout=2) == 'keep'
    assert calls == [['keep']]


@pytest.mark.parametrize('bad_result', [[], RuntimeError('provider failed')])
def test_batch_failure_reaches_all_waiters(monkeypatch, bad_result):
    import llm
    def batch(provider, inputs):
        if isinstance(bad_result, Exception): raise bad_result
        return bad_result
    monkeypatch.setattr(llm, 'batch', batch, raising=False)
    state = {'batch_id':'fail', 'llm_batch_size':2}
    futures = [provider_batch.submit(i, state) for i in range(2)]
    for future in futures:
        with pytest.raises((ValueError, RuntimeError)): future.result(timeout=2)


def test_framework_transport_and_parser(monkeypatch):
    import asyncio
    import llm
    from langchain_core.messages import AIMessage
    from evoagentx.models.openai_model import OpenAILLM, OpenAILLMConfig
    calls = []
    def batch(provider, inputs):
        calls.append(inputs)
        return [AIMessage(content='hello') for _ in inputs]
    monkeypatch.setattr(llm, 'batch', batch, raising=False)
    model = OpenAILLM(OpenAILLMConfig(model='gpt-4o-mini', openai_key='test'))
    provider_batch.attach_workflow_model(model, {'batch_id':'framework','llm_batch_size':2})
    result = asyncio.run(model.async_generate(prompt='question', system_message='system', parse_mode='str'))
    assert result is not None
    assert calls[0][0][0]['role'] == 'system'
    assert calls[0][0][-1]['content'] == 'question'


def test_deep_agent_tools_and_response_preserved(monkeypatch):
    import llm
    from langchain_core.messages import AIMessage, HumanMessage
    calls = []
    response = AIMessage(content='', tool_calls=[{'name':'lookup','args':{},'id':'call-1'}])
    def batch(provider, inputs, **kwargs):
        calls.append((inputs, kwargs))
        return [response for _ in inputs]
    monkeypatch.setattr(llm, 'batch', batch, raising=False)
    model = provider_batch.agent_model({'batch_id':'deep','llm_batch_size':2})
    tool = {'type':'function', 'function':{'name':'lookup','description':'Lookup','parameters':{'type':'object','properties':{}}}}
    result = model.bind_tools([tool]).invoke([HumanMessage(content='lookup')])
    assert result.tool_calls == response.tool_calls
    assert calls[0][1]['tools'][0]['function']['name'] == 'lookup'


def test_distinct_options_and_batches_never_mix(monkeypatch):
    import llm
    calls = []
    def batch(provider, inputs, **kwargs):
        calls.append((inputs, kwargs))
        return inputs
    monkeypatch.setattr(llm, 'batch', batch, raising=False)
    a = {'batch_id':'a','llm_batch_size':5}
    b = {'batch_id':'b','llm_batch_size':5}
    futures = [provider_batch.submit('a', a), provider_batch.submit('b', b),
               provider_batch.submit('tool', a, temperature=0.2)]
    assert [f.result(timeout=2) for f in futures] == ['a','b','tool']
    assert len(calls) == 3


def test_framework_native_tool_response_conversion():
    from langchain_core.messages import AIMessage
    import json, re
    message = AIMessage(content='', tool_calls=[{'id':'t1','name':'lookup','args':{'q':'x'}}])
    text = provider_batch._text(message)
    calls = json.loads(re.search(r'<tool_call>(.*?)</tool_call>', text).group(1))
    assert calls == [{'id':'t1','function_name':'lookup','function_args':{'q':'x'}}]


def test_workflow_runner_uses_batch_after_agent_reconstruction(monkeypatch):
    import llm
    from backend.api import runner
    from conftest import make_task, make_graph
    from evoagentx.models.openai_model import OpenAILLM, OpenAILLMConfig
    calls = []
    def batch(provider, inputs):
        calls.append((provider, inputs))
        return ['<answer>done</answer>' for _ in inputs]
    monkeypatch.setattr(llm, 'batch', batch, raising=False)
    monkeypatch.setattr(runner, '_make_llm', lambda: OpenAILLM(OpenAILLMConfig(model='gpt-4o-mini', openai_key='test')))
    graph = make_graph([make_task('answer', inputs=['question'], outputs=['answer'])])
    run_id = runner.start_run(graph, {'question':'hello'}, background=False,
                              batch_id='reconstructed', llm_batch_size=4)
    result = runner.get_run(run_id)
    assert result['status'] == 'success', result.get('error')
    assert calls and all(provider == 'safechain' for provider, _ in calls)
