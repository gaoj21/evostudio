from types import SimpleNamespace
from backend.features.execution.token_usage import reported_usage, attach_model


def test_missing_usage_is_unknown_and_reported_usage_normalizes():
    assert reported_usage(None) is None
    assert reported_usage({'prompt_tokens':12,'completion_tokens':3}) == {'input_tokens':12,'output_tokens':3,'total_tokens':15}
    assert reported_usage({'input_tokens':True,'output_tokens':3}) is None


def test_model_observer_is_scoped_and_not_attached_twice():
    class Model:
        def _update_cost(self, response): return 'original return'
    state={}; model=Model()
    attach_model(model,state);attach_model(model,state)
    response=SimpleNamespace(usage=SimpleNamespace(prompt_tokens=12,completion_tokens=3))
    assert model._update_cost(response) == 'original return'
    assert state['token_usage']['total_tokens'] == 15
    assert state['token_usage']['reported_calls'] == 1
    other={};attach_model(Model(),other)
    assert other == {}
