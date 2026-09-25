"""Evaluation off the canvas: the workflow's evaluators, their routes, and the
draft that survives leaving the panel.

An evaluator is Python code kept on the graph document. Nothing about it is a
node any more: an old canvas evaluator is lifted into `graph["evaluators"]` on
load, and the Evaluate & Evolve panel owns the list from there.
"""
import json

import pytest
from fastapi.testclient import TestClient

from backend.api import graphs as graph_store
from backend.api.app import app
from backend.features.evaluation import evaluator_tools as tools

FACTORY = '''def build_evaluator(threshold: float = 0.5, label_field: str = "expected"):
    class Evaluator:
        def evaluate(self, records):
            return {"metrics": {"score": float(len(records) >= threshold)}}
    return Evaluator()
'''

FUNCTION = '''def evaluate(records, decimals: int = 2):
    return {"metrics": {"count": round(len(records), decimals)}}
'''


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(graph_store, 'GRAPHS_DIR', tmp_path / 'graphs')
    monkeypatch.setattr(tools, 'DRAFTS_DIR', tmp_path / 'drafts')
    return TestClient(app)


@pytest.fixture
def workflow(client):
    created = client.post('/api/graphs', json={'name': 'Panel', 'goal': 'g'}).json()
    body = {**created, 'tasks': [{'name': 'work', 'description': 'd', 'prompt': 'do {text}',
                                  'parse_mode': 'str',
                                  'inputs': [{'name': 'text', 'type': 'str', 'description': 't', 'required': True}],
                                  'outputs': [{'name': 'answer', 'type': 'str', 'description': 'a', 'required': True}]}],
            'edges': []}
    assert client.put(f"/api/graphs/{created['id']}", json=body).status_code == 200
    return created['id']


# ---------------------------------------------------------------------------
# Migration: the canvas loses its evaluator nodes, once
# ---------------------------------------------------------------------------

def risk_shaped_graph():
    """The shape of the user's live monitoring workflow: one evaluator node,
    wired to nothing, holding the whole report as pasted code."""
    return {
        'id': 'credit-risk-monitoring', 'name': 'Monitoring', 'goal': 'watch',
        'flow_version': 2,
        'tasks': [
            {'name': 'feed', 'kind': 'source', 'source': {'type': 'dataloader', 'resource_id': 'r'},
             'inputs': [], 'outputs': [{'name': 'company', 'type': 'str'}]},
            {'name': 'decide', 'prompt': 'decide {company}', 'description': 'decide',
             'inputs': [{'name': 'company', 'type': 'str'}],
             'outputs': [{'name': 'decision', 'type': 'str'}]},
            {'name': 'monitoring_report', 'kind': 'evaluator', 'enabled': True,
             'description': 'Caught / missed, lead days', 'inputs': [], 'outputs': [],
             'x': 1611, 'y': 178,
             'evaluator': {'type': 'python', 'code': FUNCTION, 'config': {},
                           'timing': 'batch', 'timeout': 120, 'metric': 'count',
                           'direction': 'maximize',
                           'labels': {'resource_id': '7a3264d9423f408a88915e75262d0a79',
                                      'loader': 'auto'}}}],
        'edges': [{'source': 'feed', 'target': 'decide',
                   'mappings': [{'from': 'company', 'to': 'company'}]}]}


class TestAnEvaluatorNodeIsLifted:
    def test_the_real_risk_graphs_shape_becomes_an_entry(self):
        migrated = graph_store.migrate_flow(risk_shaped_graph())
        assert [t['name'] for t in migrated['tasks']] == ['feed', 'decide']
        [entry] = migrated['evaluators']
        assert entry['name'] == 'monitoring_report'
        assert entry['code'] == FUNCTION
        assert entry['timing'] == 'batch' and entry['timeout'] == 120
        assert entry['metric'] == 'count' and entry['direction'] == 'maximize'
        assert entry['labels'] == {'resource_id': '7a3264d9423f408a88915e75262d0a79', 'loader': 'auto'}
        assert entry['enabled'] is True
        assert entry['description'] == 'Caught / missed, lead days'
        assert migrated['flow_version'] == graph_store.FLOW_VERSION

    def test_the_old_node_timing_becomes_after_each_run(self):
        old = risk_shaped_graph()
        old['tasks'][-1]['evaluator']['timing'] = 'node'
        old['edges'].append({'source': 'decide', 'target': 'monitoring_report',
                             'mappings': [{'from': 'decision', 'to': 'prediction'}]})
        migrated = graph_store.migrate_flow(old)
        assert migrated['evaluators'][0]['timing'] == 'run'
        # Every edge that touched the node is gone with it.
        assert [(e['source'], e['target']) for e in migrated['edges']] == [('feed', 'decide')]

    def test_it_is_idempotent(self):
        once = graph_store.migrate_flow(risk_shaped_graph())
        twice = graph_store.migrate_flow(json.loads(json.dumps(once)))
        assert twice == once
        assert graph_store.lift_evaluators(json.loads(json.dumps(once))) == once

    def test_a_graph_from_before_edge_mappings_is_migrated_both_ways(self):
        old = risk_shaped_graph()
        old.pop('flow_version')
        old['edges'] = [{'source': 'feed', 'target': 'decide'}]
        migrated = graph_store.migrate_flow(old)
        assert migrated['edges'][0]['mappings'] == [{'from': 'company', 'to': 'company'}]
        assert [e['name'] for e in migrated['evaluators']] == ['monitoring_report']

    def test_loading_a_saved_old_graph_lifts_it(self, client, tmp_path):
        graph_store.GRAPHS_DIR.mkdir(parents=True, exist_ok=True)
        (graph_store.GRAPHS_DIR / 'credit-risk-monitoring.json').write_text(
            json.dumps(risk_shaped_graph()), encoding='utf-8')
        loaded = graph_store.load_graph('credit-risk-monitoring')
        assert not any(t.get('kind') == 'evaluator' for t in loaded['tasks'])
        assert loaded['evaluators'][0]['name'] == 'monitoring_report'
        # And the run plan no longer has anything to say about it.
        plan = client.post('/api/graphs/credit-risk-monitoring/run-plan',
                           json={'start_at': [], 'mode': 'single'})
        assert plan.status_code == 200, plan.text
        assert 'monitoring_report' not in [n['name'] for n in plan.json()['nodes']]


# ---------------------------------------------------------------------------
# Storage and validation
# ---------------------------------------------------------------------------

class TestStoringTheList:
    def test_put_and_get_and_delete(self, client, workflow):
        entry = {'name': 'quality', 'code': FACTORY, 'config': {'threshold': 1.0},
                 'metric': 'score', 'timing': 'batch'}
        put = client.put(f'/api/graphs/{workflow}/evaluators', json={'evaluators': [entry]})
        assert put.status_code == 200, put.text
        [stored] = put.json()['evaluators']
        assert stored['name'] == 'quality' and stored['timeout'] == tools.DEFAULT_TIMEOUT
        assert stored['direction'] == 'maximize' and stored['enabled'] is True
        assert client.get(f'/api/graphs/{workflow}/evaluators').json()['evaluators'] == [stored]
        # It is on the graph document, not among the tasks.
        saved = graph_store.load_graph(workflow)
        assert saved['evaluators'] == [stored] and saved['tasks'][0]['name'] == 'work'
        assert client.delete(f'/api/graphs/{workflow}/evaluators/quality').json()['evaluators'] == []
        assert client.delete(f'/api/graphs/{workflow}/evaluators/quality').status_code == 404

    def test_saving_the_canvas_keeps_the_evaluators(self, client, workflow):
        client.put(f'/api/graphs/{workflow}/evaluators',
                   json={'evaluators': [{'name': 'quality', 'code': FUNCTION, 'metric': 'count'}]})
        body = graph_store.load_graph(workflow)
        body.pop('evaluators')           # the canvas does not send them at all
        body['tasks'][0]['prompt'] = 'do it differently {text}'
        saved = client.put(f'/api/graphs/{workflow}', json=body)
        assert saved.status_code == 200, saved.text
        assert [e['name'] for e in saved.json()['evaluators']] == ['quality']
        assert [e['name'] for e in graph_store.load_graph(workflow)['evaluators']] == ['quality']
        # An empty list still means "none", so they can be cleared.
        cleared = client.put(f'/api/graphs/{workflow}', json={**body, 'evaluators': []})
        assert cleared.json()['evaluators'] == []

    @pytest.mark.parametrize('entry,message', [
        ({'name': '', 'code': FUNCTION}, 'needs a name'),
        ({'name': 'a', 'code': 'def evaluate(:'}, 'syntax error'),
        ({'name': 'a', 'code': FUNCTION, 'timing': 'node'}, 'timing must be one of'),
        ({'name': 'a', 'code': FUNCTION, 'timeout': 5}, 'time limit'),
        ({'name': 'a', 'code': FUNCTION, 'timeout': 4000}, 'time limit'),
        ({'name': 'a', 'code': FUNCTION, 'direction': 'sideways'}, 'maximize or minimize'),
        ({'name': 'a', 'code': FUNCTION, 'config': []}, 'object'),
    ])
    def test_what_a_save_refuses(self, client, workflow, entry, message):
        result = client.put(f'/api/graphs/{workflow}/evaluators', json={'evaluators': [entry]})
        assert result.status_code == 422
        assert message in result.json()['detail']
        assert graph_store.load_graph(workflow)['evaluators'] == []

    def test_duplicate_names_are_refused(self, client, workflow):
        entry = {'name': 'quality', 'code': FUNCTION}
        result = client.put(f'/api/graphs/{workflow}/evaluators', json={'evaluators': [entry, entry]})
        assert result.status_code == 422 and 'unique' in result.json()['detail']

    def test_unknown_keys_are_not_kept(self, client, workflow):
        client.put(f'/api/graphs/{workflow}/evaluators',
                   json={'evaluators': [{'name': 'quality', 'code': FUNCTION, 'x': 10, 'kind': 'evaluator'}]})
        [stored] = graph_store.load_graph(workflow)['evaluators']
        assert set(stored) == set(tools.ENTRY_KEYS) - {'description'}


# ---------------------------------------------------------------------------
# Interface discovery: the code declares the form
# ---------------------------------------------------------------------------

class TestInterfaceDiscovery:
    def test_a_factory_declares_its_parameters(self, client):
        body = client.post('/api/evaluators/interface', json={'code': FACTORY}).json()
        assert body['kind'] == 'factory' and body['error'] is None
        assert [(p['name'], p['type'], p['default'], p['required']) for p in body['params']] == [
            ('threshold', 'float', 0.5, False), ('label_field', 'str', 'expected', False)]
        # records are handed to the object's evaluate(); a factory is not asked for them.
        assert body['provided'] == ['label_records']

    def test_a_plain_function_declares_its_parameters(self, client):
        body = client.post('/api/evaluators/interface', json={'code': FUNCTION}).json()
        assert body['kind'] == 'function'
        assert [p['name'] for p in body['params']] == ['decimals']

    def test_a_syntax_error_is_part_of_the_answer(self, client):
        body = client.post('/api/evaluators/interface', json={'code': 'def evaluate(:'}).json()
        assert body['kind'] is None and body['params'] == []
        assert 'syntax error on line 1' in body['error']

    def test_no_entrypoint_says_what_to_define(self, client):
        body = client.post('/api/evaluators/interface', json={'code': 'x = 1'}).json()
        assert body['kind'] is None and 'Define evaluate' in body['error']

    def test_an_untyped_parameter_is_offered_as_any(self, client):
        body = client.post('/api/evaluators/interface',
                           json={'code': 'def build_evaluator(field=None, typed: int = 3):\n    return None'}).json()
        assert [(p['name'], p['type'], p['required']) for p in body['params']] == [
            ('field', 'any', False), ('typed', 'int', False)]

    def test_untyped_required_parameters_are_asked_for(self, client):
        from backend.features.evaluation.python_evaluator import interface
        schema = interface('def build_evaluator(needed):\n    return None')
        assert schema['inputs'][0] == {'name': 'needed', 'type': 'any', 'required': True,
                                       'origin': 'parameter'}

    def test_the_credit_risk_report_still_declares_its_form(self):
        from pathlib import Path
        code = (Path(__file__).resolve().parents[2]
                / 'projects/credit_risk/evaluators/monitoring_report.py').read_text()
        described = tools.described(code)
        assert described['error'] is None and described['kind'] == 'factory'
        assert {'decision_field', 'trajectory_field', 'date_field', 'label_key',
                'reasoning_nodes'} <= {p['name'] for p in described['params']}
        # label_records is supplied by the platform, never asked for.
        assert 'label_records' not in {p['name'] for p in described['params']}
        assert 'label_records' in described['provided']


# ---------------------------------------------------------------------------
# Preview and run
# ---------------------------------------------------------------------------

@pytest.fixture
def saved_run(client, workflow, monkeypatch):
    from backend.api import runner
    from backend.features.chat import chat_control
    from backend.features.evaluation.python_evaluator import execute_with_logs
    real = chat_control.worker
    monkeypatch.setattr(chat_control, 'worker',
                        lambda kind, payload, **kw: execute_with_logs(payload)
                        if kind == 'evaluate_python' else real(kind, payload, **kw))
    runner._runs.clear()
    runner._runs['run-1'] = {'run_id': 'run-1', 'graph_id': workflow, 'status': 'success',
                             'inputs': {'text': 'x'}, 'result': {'answer': 'y'},
                             'nodes': [{'name': 'work', 'status': 'completed', 'output': {'answer': 'y'}}]}
    return 'run-1'


class TestPreviewAndRun:
    def test_a_preview_saves_nothing(self, client, workflow, saved_run):
        body = {'code': FUNCTION, 'config': {'decimals': 0}, 'run_id': saved_run}
        result = client.post(f'/api/graphs/{workflow}/evaluators/preview', json=body)
        assert result.status_code == 200, result.text
        assert result.json()['report']['metrics'] == {'count': 1}
        assert [m['name'] for m in result.json()['metrics']] == ['count']
        assert result.json()['execution_count'] == 1
        assert graph_store.load_graph(workflow)['evaluators'] == []
        from backend.api import runner
        assert not runner.get_run(saved_run).get('evaluations')

    def test_a_preview_of_broken_code_says_so_without_saving(self, client, workflow, saved_run):
        result = client.post(f'/api/graphs/{workflow}/evaluators/preview',
                             json={'code': 'def evaluate(:', 'run_id': saved_run})
        assert result.status_code == 422 and 'syntax error' in result.json()['detail']

    def test_a_run_keeps_the_report_with_that_run(self, client, workflow, saved_run):
        client.put(f'/api/graphs/{workflow}/evaluators',
                   json={'evaluators': [{'name': 'quality', 'code': FUNCTION,
                                         'metric': 'count', 'timing': 'manual'}]})
        result = client.post(f'/api/graphs/{workflow}/evaluators/run',
                             json={'name': 'quality', 'run_id': saved_run})
        assert result.status_code == 200, result.text
        assert result.json()['evaluations']['quality']['metrics'] == {'count': 1}
        from backend.api import runner
        assert runner.get_run(saved_run)['evaluations']['quality']['status'] == 'success'

    def test_a_run_of_unsaved_code_is_allowed_and_kept(self, client, workflow, saved_run):
        result = client.post(f'/api/graphs/{workflow}/evaluators/run',
                             json={'name': 'scratch', 'code': FUNCTION, 'config': {},
                                   'run_id': saved_run})
        assert result.status_code == 200, result.text
        from backend.api import runner
        assert 'scratch' in runner.get_run(saved_run)['evaluations']
        assert graph_store.load_graph(workflow)['evaluators'] == []

    def test_naming_an_evaluator_that_is_not_there(self, client, workflow, saved_run):
        result = client.post(f'/api/graphs/{workflow}/evaluators/run',
                             json={'name': 'missing', 'run_id': saved_run})
        assert result.status_code == 404

    def test_a_run_of_another_workflows_run_is_refused(self, client, workflow, saved_run):
        from backend.api import runner
        runner._runs[saved_run]['graph_id'] = 'somewhere-else'
        result = client.post(f'/api/graphs/{workflow}/evaluators/preview',
                             json={'code': FUNCTION, 'run_id': saved_run})
        assert result.status_code == 404


# ---------------------------------------------------------------------------
# Drafts
# ---------------------------------------------------------------------------

class TestTheDraft:
    def test_it_survives_leaving_the_panel_and_a_restart(self, client, workflow):
        assert client.get(f'/api/graphs/{workflow}/evaluators/draft').json()['draft'] is None
        put = client.put(f'/api/graphs/{workflow}/evaluators/draft',
                         json={'code': FUNCTION, 'config': {'decimals': 1}})
        assert put.status_code == 200, put.text
        assert put.json()['draft']['updated_at']
        # A restart: nothing of it is in memory, it is read back from disk.
        fresh = TestClient(app)
        draft = fresh.get(f'/api/graphs/{workflow}/evaluators/draft').json()['draft']
        assert draft['code'] == FUNCTION and draft['config'] == {'decimals': 1}
        assert (tools.DRAFTS_DIR / f'{workflow}.json').is_file()

    def test_it_is_never_validated_and_never_run(self, client, workflow, monkeypatch):
        from backend.features.chat import chat_control
        monkeypatch.setattr(chat_control, 'worker',
                            lambda *a, **kw: pytest.fail('a draft must not be run'))
        put = client.put(f'/api/graphs/{workflow}/evaluators/draft',
                         json={'code': 'def evaluate(:  # half typed'})
        assert put.status_code == 200, put.text
        # It is not an evaluator of the workflow, so nothing evaluates with it.
        assert graph_store.load_graph(workflow)['evaluators'] == []
        assert tools.evaluate_runs(graph_store.load_graph(workflow), [{'status': 'success'}]) == {}

    def test_saving_that_same_code_clears_it(self, client, workflow):
        client.put(f'/api/graphs/{workflow}/evaluators/draft', json={'code': FUNCTION})
        client.put(f'/api/graphs/{workflow}/evaluators',
                   json={'evaluators': [{'name': 'other', 'code': FACTORY}]})
        assert client.get(f'/api/graphs/{workflow}/evaluators/draft').json()['draft'] is not None
        client.put(f'/api/graphs/{workflow}/evaluators',
                   json={'evaluators': [{'name': 'other', 'code': FACTORY},
                                        {'name': 'kept', 'code': FUNCTION}]})
        assert client.get(f'/api/graphs/{workflow}/evaluators/draft').json()['draft'] is None

    def test_one_draft_per_workflow(self, client, workflow):
        other = client.post('/api/graphs', json={'name': 'Other', 'goal': 'g'}).json()['id']
        client.put(f'/api/graphs/{workflow}/evaluators/draft', json={'code': FUNCTION})
        client.put(f'/api/graphs/{other}/evaluators/draft', json={'code': FACTORY})
        assert client.get(f'/api/graphs/{workflow}/evaluators/draft').json()['draft']['code'] == FUNCTION
        assert client.get(f'/api/graphs/{other}/evaluators/draft').json()['draft']['code'] == FACTORY

    def test_an_unknown_workflow_has_no_draft(self, client):
        assert client.get('/api/graphs/nope/evaluators/draft').status_code == 404


def test_the_catalog_says_what_an_evaluator_is(client):
    body = client.get('/api/evaluators').json()
    assert [e['name'] for e in body['entrypoints']] == ['build_evaluator', 'evaluate']
    assert [t['id'] for t in body['timings']] == list(tools.TIMINGS)
    assert body['defaults']['timing'] == tools.DEFAULT_TIMING
    assert tools.described(body['example_code'])['error'] is None
