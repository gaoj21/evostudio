import json
import pytest
from fastapi import HTTPException
from backend.api import result_chat


def rows():
    return [{'run_id': str(i), 'graph_id': 'g', 'batch_id': 'b' if i < 25 else None,
             'status': 'success', 'result': {'decision': json.dumps({'score': i, 'risk': 'high' if i > 20 else 'low'})},
             'nodes': [{'name': 'load', 'output': {'company': 'A'}}]} for i in range(30)]


def test_aggregate_uses_all_records_and_decodes_json():
    result = result_chat.operate(rows(), {'op': 'aggregate', 'field': 'result.decision.score'})
    assert result['numeric_count'] == 30
    assert result['sum'] == 435
    assert result['mean'] == 14.5
    assert result_chat.operate(rows(), {'op': 'aggregate', 'filters': {'result.decision.risk': 'high'}})['matched_records'] == 9
    assert result_chat.operate(rows(), {'op': 'aggregate', 'field': 'steps.load.company'})['distinct_count'] == 1


def test_missing_values_not_zero_and_reads_are_scoped():
    result = result_chat.operate(rows(), {'op': 'aggregate', 'field': 'missing'})
    assert result['numeric_count'] == 0 and result['mean'] is None and result['missing'] == 30
    with pytest.raises(ValueError):
        result_chat.operate(rows(), {'op': 'read_run', 'run_id': 'outside'})
    with pytest.raises(ValueError):
        result_chat.operate(rows(), {'op': 'run_workflow'})


def test_conversation_scopes_and_calculation_observation(monkeypatch):
    monkeypatch.setattr(result_chat, 'records', lambda graph_id: rows())
    calls = []
    def ask(messages):
        calls.append(list(messages))
        return json.dumps({'op': 'aggregate', 'field': 'result.decision.score'} if len(calls) == 1 else {'reply': 'Average is 12.'})
    monkeypatch.setattr(result_chat, '_ask', ask)
    response = result_chat.chat_results('g', {'message': 'Average?', 'scope': 'batch', 'run_id': '0', 'history': [{'role': 'user', 'content': 'Look at scores'}]})
    assert response['count'] == 25
    assert response['activity'][0]['result']['mean'] == 12
    assert any(m['content'] == 'Look at scores' for m in calls[0])
    with pytest.raises(HTTPException) as exc:
        result_chat.chat_results('g', {'message': 'Read', 'run_id': 'foreign'})
    assert exc.value.status_code == 404


def test_result_listing_removes_recent_twenty_cap(monkeypatch):
    def list_runs(graph_id, limit=20):
        assert graph_id == 'g' and limit is None
        return rows()
    monkeypatch.setattr(result_chat.runner, 'list_runs', list_runs)
    assert len(result_chat.list_results('g')) == 30


def test_computed_accuracy_and_model_repairs_failed_code(monkeypatch):
    monkeypatch.setattr(result_chat, 'records', lambda graph_id: [
        {'run_id': 'a', 'inputs': {'label': 1}, 'result': {'prediction': 1}},
        {'run_id': 'b', 'inputs': {'label': 0}, 'result': {'prediction': 1}},
        {'run_id': 'c', 'inputs': {}, 'result': {'prediction': 1}},
    ])
    calls = []
    def ask(messages):
        calls.append(list(messages))
        if len(calls) == 1:
            return json.dumps({'op': 'compute', 'code': 'print(undefined_name)'})
        if len(calls) == 2:
            assert 'NameError' in messages[-1]['content']
            return json.dumps({'op': 'compute', 'code': '''import json
valid = [r for r in records if "label" in r["inputs"]]
correct = sum(r["inputs"]["label"] == r["result"]["prediction"] for r in valid)
print(json.dumps({"accuracy": correct/len(valid), "correct": correct, "evaluated": len(valid), "excluded": len(records)-len(valid)}))'''})
        assert '0.5' in messages[-1]['content']
        return json.dumps({'reply': 'Accuracy 50%, 1/2 correct; 1 missing label excluded.'})
    monkeypatch.setattr(result_chat, '_ask', ask)
    response = result_chat.chat_results('g', {'message': 'Compute accuracy', 'scope': 'all'})
    assert len(response['activity']) == 2
    assert response['activity'][0]['result']['status'] == 'error'
    assert response['activity'][1]['result']['status'] == 'success'
    assert json.loads(response['activity'][1]['result']['stdout']) == {'accuracy': .5, 'correct': 1, 'evaluated': 2, 'excluded': 1}


def test_batch_truth_is_joined_by_run_id(monkeypatch):
    monkeypatch.setattr(result_chat, 'records', lambda g: [{'run_id': 'r', 'batch_id': 'b', 'inputs': {}}])
    monkeypatch.setattr(result_chat.batch, 'get_batch', lambda b: {'graph_id': 'g', 'items': [
        {'run_id': 'other', 'label': 'wrong'}, {'run_id': 'r', 'label': 'yes', 'inputs': {'sample_json': '{"label":true}'}}]})
    answers = iter([json.dumps({'op':'compute','code':'print(records[0]["expected_label"], records[0]["source_inputs"]["sample_json"]["label"])'}), json.dumps({'reply':'yes'})])
    monkeypatch.setattr(result_chat, '_ask', lambda m: next(answers))
    result = result_chat.chat_results('g', {'scope': 'all', 'message': 'Read labels'})
    assert result['activity'][0]['result']['stdout'].strip() == 'yes True'


def test_migrated_batch_context_uses_batch_id_and_stays_scoped(monkeypatch):
    monkeypatch.setattr(result_chat, 'records', lambda g: rows())
    monkeypatch.setattr(result_chat.batch, 'get_batch', lambda b: None)
    selected = result_chat.scoped_records('g', 'batch', batch_id='b')
    assert len(selected) == 25
    with pytest.raises(HTTPException) as exc:
        result_chat.scoped_records('g', 'batch', batch_id='missing')
    assert exc.value.status_code == 404
