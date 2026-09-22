import json
import pytest
from backend.api.chat_engine import ChatEngine
from backend.api import chat_api, result_chat


def test_shared_loop_repairs_tools_and_keeps_history():
    answers = iter([{'op':'compute','code':'bad'}, {'op':'compute','code':'good'}, {'reply':'0.5'}])
    messages_seen = []
    def ask(messages):
        messages_seen.append(list(messages))
        return json.dumps(next(answers))
    engine = ChatEngine(ask, 'context', [{'role':'user','content':'previous'}], 'accuracy')
    result = engine.run(lambda ops: {'observations': [{'status':'error' if ops[0]['code'] == 'bad' else 'success'}]})
    assert result['reply'] == '0.5'
    assert len(engine.turns) == 2
    assert 'error' in messages_seen[1][-1]['content']
    assert messages_seen[0][1]['content'] == 'previous'


def test_pending_actions_stop_before_another_model_call():
    calls = []
    engine = ChatEngine(lambda m: calls.append(m) or '{"operations":[{"op":"run_workflow"}]}', 'context', [], 'run')
    engine.run(lambda ops: {'stop':True, 'observations':['proposed run']})
    assert len(calls) == 1


@pytest.mark.parametrize('context', ['canvas', 'results'])
def test_same_endpoint_and_computation_in_both_pages(monkeypatch, context):
    from conftest import make_graph, make_task
    graph = make_graph([make_task('a')])
    monkeypatch.setattr(chat_api.graph_store, 'graph_exists', lambda g: True)
    monkeypatch.setattr(chat_api, '_validate', lambda g: [])
    monkeypatch.setattr(result_chat, 'records', lambda g: [{'run_id':'r','inputs':{'truth':1},'result':{'prediction':1}}])
    answers = iter([{'op':'compute','code':'print(sum(r["inputs"]["truth"] == r["result"]["prediction"] for r in records)/len(records))'}, {'reply':'Accuracy is 1.0'}])
    def ask(messages):
        return json.dumps(next(answers))
    monkeypatch.setattr(chat_api, '_ask', ask)
    monkeypatch.setattr(result_chat, '_ask', ask)
    response = chat_api.assistant('g', {'context':context,'graph':graph,'scope':'all','message':'accuracy'})
    assert response['reply'] == 'Accuracy is 1.0'
    assert response['activity'][0]['result']['stdout'].strip() == '1.0'
    if context == 'canvas':
        assert response['applied'] is False
