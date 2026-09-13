import pytest
from backend.features.execution import provider_batch


@pytest.mark.parametrize('value', [0, -1, True, 1.5, 'bad', 1025])
def test_invalid_sizes(value):
    with pytest.raises(ValueError): provider_batch.batch_size(value)


def test_factory_contract_and_standard_compatibility(monkeypatch):
    from backend.api import runner
    called = []
    monkeypatch.setattr(runner, '_make_llm', lambda: 'legacy')
    assert runner._model_for_run({}) == 'legacy'
    def native(provider=None, *, batch_size, batch_id):
        called.append((batch_size, batch_id))
        return 'native'
    monkeypatch.setattr(runner, 'get_evoagentx_llm', native)
    assert runner._model_for_run({'llm_batch_size':16, 'batch_id':'group-1'}) == 'native'
    assert called == [(16,'group-1')]
    monkeypatch.setattr(runner, 'get_evoagentx_llm', lambda: 'single')
    with pytest.raises(ValueError, match='not connected'):
        runner._model_for_run({'llm_batch_size':16, 'batch_id':'group-1'})


def test_native_batch_persists_and_forwards_options(monkeypatch):
    from backend.api import batch, runner
    import llm
    monkeypatch.setattr(llm, 'get_evoagentx_llm', lambda **kwargs: None)
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


def test_api_rejects_unready_adapter_before_start():
    # Check availability without instantiating a model or making a network call.
    with pytest.raises(ValueError, match='not connected'):
        provider_batch.require_factory(lambda provider=None: None)
