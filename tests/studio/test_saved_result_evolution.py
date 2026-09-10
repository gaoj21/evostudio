import json
from unittest.mock import Mock
from types import SimpleNamespace
import pytest
from backend.api import saved_result_evolution as saved, sources, evolve_api, runner, batch


def test_resolve_saved_run_never_replays(monkeypatch):
    run = {'run_id': 'r', 'graph_id': 'g', 'status': 'success', 'inputs': {'answer': 'yes'}, 'result': 'yes'}
    monkeypatch.setattr(runner, 'get_run', lambda _: run)
    start = Mock(side_effect=AssertionError('must not run'))
    monkeypatch.setattr(runner, 'start_run', start)
    records, source = saved.resolve('g', {'source': 'saved_run', 'run_id': 'r', 'label_key': 'answer'})
    result = saved.evaluate(records, 'exact_match', source)
    assert result['metrics']['score'] == 1
    start.assert_not_called()
    with pytest.raises(sources.SourceError):
        saved.resolve('other', {'source': 'saved_run', 'run_id': 'r'})


def test_missing_labels_are_unscored():
    r = {'id': '0', 'status': 'success', 'inputs': {}, 'prediction': 'low', 'label': None}
    assert saved.evaluate([r], 'exact_match', {})['metrics'] == {'score': None, 'scored': 0, 'total': 1, 'unscored': 1}


def test_saved_engine_proposes_without_runner_or_optimizer(tmp_path, monkeypatch):
    records = [{'id': '0', 'run_id': 'r', 'status': 'success', 'inputs': {}, 'label': 'yes', 'prediction': 'no', 'nodes': []}]
    (tmp_path / 'dataset.jsonl').write_text(json.dumps(records[0]) + '\n')
    state = {'task_id': 'saved', 'created_at': evolve_api._utcnow()}
    monkeypatch.setitem(evolve_api._tasks, 'saved', state)
    monkeypatch.setattr(evolve_api, '_task_dir', lambda _: tmp_path)
    start = Mock(side_effect=AssertionError('must not replay'))
    monkeypatch.setattr(runner, 'start_run', start)
    llm = SimpleNamespace(generate=Mock(return_value=SimpleNamespace(content='{"prompts":{"a":"Improved prompt"}}')))
    monkeypatch.setattr(runner, '_make_llm', lambda: llm)
    graph = {'id': 'g', 'tasks': [{'name': 'a', 'prompt': 'Original'}]}
    evolve_api._execute_evolve('saved', graph, 'exact_match', {'mode': 'evolve_evaluate', 'source': {'type': 'saved_run'}, 'nodes': ['a']}, tmp_path)
    assert state['status'] == 'done', state.get('error')
    assert state['baseline']['metrics']['score'] == 0
    assert state['validation_status'] == 'not_run'
    assert not state.get('optimized')
    assert graph['tasks'][0]['prompt'] == 'Original'
    llm.generate.assert_called_once()
    start.assert_not_called()


def test_saved_route_does_not_load_dataset_or_start_workflow(monkeypatch):
    from fastapi.testclient import TestClient
    from backend.api import app, graphs
    monkeypatch.setattr(graphs, 'load_graph', lambda _: {'id': 'g', 'tasks': [{'name': 'a'}]})
    monkeypatch.setattr(graphs, 'validate_graph', lambda _: ([{'name': 'a'}], []))
    records = [{'id': '0', 'inputs': {}, 'prediction': 'yes', 'label': 'yes', 'status': 'success'}]
    monkeypatch.setattr(saved, 'resolve', lambda *a: (records, {'type': 'saved_run', 'run_id': 'r'}))
    monkeypatch.setattr(sources, 'credit_risk_records', Mock(side_effect=AssertionError('No dataset replay')))
    launch = Mock(return_value='task')
    monkeypatch.setattr(evolve_api, 'start_evolve', launch)
    response = TestClient(app.app).post('/api/graphs/g/evolve', json={'source': 'saved_run', 'run_id': 'r', 'mode': 'evolve_evaluate', 'metric': 'exact_match', 'nodes': ['a']})
    assert response.status_code == 200, response.text
    assert launch.call_args.args[1] == records
    assert launch.call_args.args[3]['source']['run_id'] == 'r'


def test_historical_evaluation_ignores_current_graph_validation_and_stale_nodes(monkeypatch):
    from fastapi.testclient import TestClient
    from backend.api import app, graphs
    monkeypatch.setattr(graphs, 'load_graph', lambda _: {'id': 'g', 'tasks': []})
    monkeypatch.setattr(graphs, 'validate_graph', Mock(side_effect=AssertionError('Current graph is irrelevant')))
    monkeypatch.setattr(saved, 'resolve', lambda *a: ([{'id': '0'}], {'type': 'saved_batch'}))
    launch = Mock(return_value='eval')
    monkeypatch.setattr(evolve_api, 'start_evolve', launch)
    res = TestClient(app.app).post('/api/graphs/g/evolve', json={'source': 'saved_batch', 'batch_id': 'b', 'mode': 'evaluate', 'nodes': ['removed_node']})
    assert res.status_code == 200, res.text
    assert launch.call_args.args[3]['nodes'] == []


def test_saved_split_filters_existing_records_without_replay(tmp_path, monkeypatch):
    from backend.api import datasets
    (tmp_path / 'partitions.jsonl').write_text('\n'.join(json.dumps(r) for r in [
        {'case_id':'devcase','partition':'dev'}, {'case_id':'testcase','partition':'test'}, {'case_id':'missing','partition':'test'}]))
    monkeypatch.setattr(datasets, 'release', lambda _: tmp_path)
    original = {'graph_id':'g','status':'succeeded','source':{'dataset':'release'}, 'items':[
        {'run_id':'a','status':'success','inputs':{'sample_id':'devcase'}},
        {'run_id':'b','status':'success','inputs':{'sample_id':'testcase'}},
        {'run_id':'c','status':'failed','inputs':{'sample_id':'testcase'}}]}
    monkeypatch.setattr(batch, 'get_batch', lambda _: original)
    monkeypatch.setattr(runner, 'get_run', lambda _: {'result':'stored','nodes':[]})
    records, selection = saved.resolve('g', {'source':'saved_batch','batch_id':'b','split':'test'})
    assert [r['run_id'] for r in records] == ['b','c']
    assert selection['matched_records'] == 2 and selection['matched_cases'] == 1
    assert selection['missing_cases'] == 1
    assert len(original['items']) == 3
    records, selection = saved.resolve('g', {'source':'saved_batch','batch_id':'b','dataset':'release','split':'dev'})
    assert [r['run_id'] for r in records] == ['a']
    with pytest.raises(sources.SourceError, match='Choose dev'):
        saved.resolve('g', {'source':'saved_batch','batch_id':'b','split':'train'})
