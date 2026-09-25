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


def test_saved_route_does_not_load_dataset_or_start_workflow(monkeypatch):
    from fastapi.testclient import TestClient
    from backend.api import app, graphs
    monkeypatch.setattr(graphs, 'load_graph', lambda _: {'id': 'g', 'tasks': [{'name': 'a'}]})
    monkeypatch.setattr(graphs, 'validate_graph', lambda _: ([{'name': 'a'}], []))
    records = [{'id': '0', 'inputs': {}, 'prediction': 'yes', 'label': 'yes', 'status': 'success'}]
    monkeypatch.setattr(saved, 'resolve', lambda *a: (records, {'type': 'saved_run', 'run_id': 'r'}))
    monkeypatch.setattr(sources, 'records_from_source_node', Mock(side_effect=AssertionError('No dataset replay')))
    launch = Mock(return_value='task')
    monkeypatch.setattr(evolve_api, 'start_evolve', launch)
    response = TestClient(app.app).post('/api/graphs/g/evolve', json={'source': 'saved_run', 'run_id': 'r', 'mode': 'evaluate', 'metric': 'exact_match'})
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


def test_saved_results_take_no_dataset_or_split(monkeypatch):
    """Saved results are evaluated as they were saved: the platform knows no
    project's dataset partitions, so a dataset/split in the body filters
    nothing (and is not an error)."""
    original = {'graph_id':'g','status':'succeeded','source':{'type':'test_ledger'}, 'items':[
        {'run_id':'a','status':'success','inputs':{'account':'x'}},
        {'run_id':'b','status':'success','inputs':{'account':'y'}},
        {'run_id':'c','status':'failed','inputs':{'account':'y'}}]}
    monkeypatch.setattr(batch, 'get_batch', lambda _: original)
    monkeypatch.setattr(runner, 'get_run', lambda _: {'result':'stored','nodes':[]})
    monkeypatch.setattr(sources, 'records_from_source_node', Mock(side_effect=AssertionError('No replay')))
    for body in ({}, {'split':'test'}, {'dataset':'release','split':'train'}):
        records, selection = saved.resolve('g', {'source':'saved_batch','batch_id':'b', **body})
        assert [r['run_id'] for r in records] == ['a','b','c']
        assert selection['matched_records'] == selection['available_records'] == 3
        assert 'matched_cases' not in selection and 'split' not in selection
        assert selection['origin'] == {'type':'test_ledger'}
    assert len(original['items']) == 3


def test_saved_results_can_only_be_evaluated(monkeypatch):
    """Prompts proposed from saved traces were never validated: Evolve
    replays the canvas Input and holds out part of it instead."""
    from fastapi.testclient import TestClient
    from backend.api import app, graphs
    monkeypatch.setattr(graphs, 'load_graph', lambda _: {'id': 'g', 'tasks': [{'name': 'a'}]})
    monkeypatch.setattr(saved, 'resolve', lambda *a: ([{'id': '0'}], {'type': 'saved_run', 'run_id': 'r'}))
    launch = Mock(return_value='task')
    monkeypatch.setattr(evolve_api, 'start_evolve', launch)
    response = TestClient(app.app).post('/api/graphs/g/evolve', json={'source': 'saved_run', 'run_id': 'r', 'mode': 'evolve_evaluate', 'nodes': ['a']})
    assert response.status_code == 422 and 'canvas Input' in response.text
    launch.assert_not_called()
