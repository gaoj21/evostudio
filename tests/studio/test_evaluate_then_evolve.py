"""Evaluation is code brought to the run; Evolve continues an evaluation.

The code an evaluation ran with stays with that evaluation — the workflow is
not changed — and an Evolve started from it optimizes one of its metrics."""
import copy
import time

import pytest
from fastapi.testclient import TestClient

from test_evaluator_evolve_audit import _evaluator_worker_in_process  # noqa: F401

CODE = '''def evaluate(records, field: str = "answer"):
    got = [((r.get("node_outputs") or {}).get("work") or {}).get(field) for r in records]
    ok = [g == "yes" for g in got]
    return {"metrics": {"accuracy": sum(ok) / len(ok), "answered": sum(g is not None for g in got)}}
'''
GRAPH = {'id': 'g', 'name': 'G', 'goal': 'g', 'flow_version': 3, 'edges': [],
         'tasks': [{'name': 'work', 'prompt': 'p {text}', 'inputs': [{'name': 'text'}], 'outputs': [{'name': 'answer'}]}],
         'evaluators': [{'name': 'kept', 'code': 'x', 'metric': 'm', 'enabled': True}]}
RUNS = [{'id': str(i), 'run_id': f'r{i}', 'status': 'success', 'inputs': {'text': 't'},
         'node_outputs': {'work': {'answer': 'yes' if i else 'no'}}, 'nodes': []} for i in range(4)]


@pytest.fixture
def app(monkeypatch, tmp_path):
    from backend.api import app as studio, graphs
    from backend.features.evaluation import evolve_api
    from backend.api import saved_result_evolution as saved
    monkeypatch.setattr(graphs, 'load_graph', lambda gid: copy.deepcopy(GRAPH) if gid == 'g' else None)
    monkeypatch.setattr(graphs, 'validate_graph', lambda g: (g['tasks'], []))
    monkeypatch.setattr(evolve_api, 'EVOLVE_DIR', tmp_path / 'evolve')
    monkeypatch.setattr(evolve_api, '_tasks', {})
    monkeypatch.setattr(saved, 'resolve', lambda gid, body: (copy.deepcopy(RUNS), {'type': 'saved_batch', 'batch_id': 'b1'}))
    kept = {}
    monkeypatch.setattr(evolve_api, '_keep_report', lambda source, reports: kept.update(source=source, reports=reports))
    return TestClient(studio.app), evolve_api, kept


def settle(evolve_api, task_id):
    for _ in range(300):
        task = evolve_api.get_task(task_id)
        if task['status'] != 'running':
            return task
        time.sleep(0.02)
    raise AssertionError('task never settled')


def test_an_evaluation_runs_the_code_it_was_given_and_keeps_it(app):
    client, evolve_api, kept = app
    started = client.post('/api/graphs/g/evolve', json={'source': 'saved_batch', 'batch_id': 'b1', 'mode': 'evaluate',
                                                         'code': CODE, 'config': {'field': 'answer'}})
    assert started.status_code == 200, started.text
    task = settle(evolve_api, started.json()['task_id'])
    assert task['status'] == 'done', task.get('error')
    report = task['baseline']['evaluations']['evaluation']
    assert report['metrics'] == {'accuracy': 0.75, 'answered': 4}
    assert task['params']['evaluator_entry']['code'] == CODE
    assert kept['source']['batch_id'] == 'b1' and 'evaluation' in kept['reports']   # on the batch too
    assert GRAPH['evaluators'][0]['name'] == 'kept'                                 # the workflow is untouched


def test_evolve_continues_an_evaluation_with_the_metric_chosen(app, monkeypatch):
    client, evolve_api, _ = app
    evaluation = settle(evolve_api, client.post('/api/graphs/g/evolve', json={
        'source': 'saved_batch', 'batch_id': 'b1', 'mode': 'evaluate', 'code': CODE}).json()['task_id'])
    from backend.features.evaluation import canvas_evolution
    seen = {}

    def settings(graph, body):
        seen['entry'] = graph['evaluators'][0]
        seen['workflow'] = graph['_workflow_evaluators']
        return {'mode': body['mode'], 'evaluator': body['evaluator'], 'source': {'type': 'canvas'}, 'nodes': ['work']}
    monkeypatch.setattr(canvas_evolution, 'settings', settings)
    monkeypatch.setattr(evolve_api, 'start_evolve', lambda graph, records, metric, params: seen.update(params=params) or 'evo')

    missing = client.post('/api/graphs/g/evolve', json={'source': 'canvas', 'mode': 'evolve', 'from_evaluation': evaluation['task_id']})
    assert missing.status_code == 422 and 'metric' in missing.text
    started = client.post('/api/graphs/g/evolve', json={'source': 'canvas', 'mode': 'evolve', 'nodes': ['work'],
                                                         'from_evaluation': evaluation['task_id'], 'metric': 'accuracy'})
    assert started.status_code == 200, started.text
    assert seen['entry']['code'] == CODE and seen['entry']['metric'] == 'accuracy'
    assert seen['workflow'][0]['name'] == 'kept'
    assert seen['params']['from_evaluation'] == evaluation['task_id']
    assert seen['params']['evaluator_entry']['metric'] == 'accuracy'


def test_evolve_only_continues_a_finished_evaluation_of_this_workflow(app):
    client, _, _ = app
    res = client.post('/api/graphs/g/evolve', json={'source': 'canvas', 'mode': 'evolve', 'from_evaluation': 'nope', 'metric': 'm'})
    assert res.status_code == 422 and 'evaluation of this workflow' in res.text
