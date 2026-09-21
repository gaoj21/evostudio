import pytest
from fastapi.testclient import TestClient
from backend.features.evaluation.python_evaluator import interface, execute
from backend.features.evaluation import evaluator_tools as evaluators
from backend.features.chat import chat_control
from backend.api import graphs, runner, batch

CODE = '''def evaluate(records, threshold: float = 0.5, label_records=None):
    return {"metrics": {"accuracy": sum(r["node_outputs"]["hidden"]["value"] >= threshold for r in records) / len(records)},
            "details": {"labels": label_records, "statuses": [r["status"] for r in records]}}
'''


def test_interface_is_static_and_excludes_supplied_records():
    result = interface(CODE + '\nraise RuntimeError("do not execute")')
    assert [f['name'] for f in result['inputs']] == ['threshold']
    assert result['inputs'][0]['default'] == .5
    assert 'records' in result['provided_inputs']


def test_typed_inputs_reject_wrong_values():
    with pytest.raises(ValueError, match='must be float'):
        execute({'code': CODE, 'records': [], 'config': {'threshold': 'bad'}})


def test_python_owns_all_records_and_unjoined_labels(monkeypatch):
    monkeypatch.setattr(chat_control, 'worker', lambda kind, payload, **kw: execute(payload))
    graph = {'tasks': [{'name': 'quality', 'kind': 'evaluator', 'evaluator': {
        'type': 'python', 'code': CODE, 'metric': 'accuracy', '_label_records': [{'x': 1}, {'x': 1}]}}], 'edges': []}
    runs = [{'status': 'success', 'node_outputs': {'hidden': {'value': 1}}},
            {'status': 'failed', 'node_outputs': {'hidden': {'value': 0}}}]
    report = evaluators.evaluate_runs(graph, runs)['quality']
    assert report['status'] == 'success'
    assert report['metrics'] == {'accuracy': .5}
    assert report['details'] == {'labels': [{'x': 1}, {'x': 1}], 'statuses': ['success', 'failed']}
    assert 'coverage' not in report


def test_preview_saved_batch_scopes_and_never_reruns(monkeypatch):
    from backend.api.app import app
    graph = {'id': 'demo', 'tasks': [{'name': 'quality', 'kind': 'evaluator', 'evaluator': {'type': 'python', 'code': CODE}}], 'edges': []}
    monkeypatch.setattr(graphs, 'load_graph', lambda _: graph)
    monkeypatch.setattr(batch, 'get_batch', lambda _: {'graph_id': 'demo', 'status': 'success', 'items': [{'run_id': 'r'}]})
    monkeypatch.setattr(runner, 'get_run', lambda _: {'status': 'success', 'node_outputs': {'hidden': {'value': 1}}})
    monkeypatch.setattr(runner, 'start_run', lambda *a, **kw: pytest.fail('Must not rerun'))
    monkeypatch.setattr(chat_control, 'worker', lambda kind, payload, **kw: execute(payload))
    client = TestClient(app)
    response = client.post('/api/evaluators/graphs/demo/preview', json={'batch_id': 'b', 'evaluator': 'quality', 'graph': graph})
    assert response.status_code == 200, response.text
    assert response.json()['report']['objective']['metric'] == 'accuracy'
    assert response.json()['execution_count'] == 1
    assert '_preview' not in graph['tasks'][0]['evaluator']
    response = client.post('/api/evaluators/graphs/another/preview', json={'batch_id': 'b', 'evaluator': 'quality', 'graph': graph})
    assert response.status_code == 404


def test_uploaded_evaluator_is_an_evolve_objective(monkeypatch):
    from backend.features.evaluation.canvas_evolution import score_saved
    monkeypatch.setattr(chat_control, 'worker', lambda kind, payload, **kw: execute(payload))
    graph = {'tasks': [{'name': 'quality', 'kind': 'evaluator', 'evaluator': {
        'type': 'python', 'code': CODE, 'metric': 'accuracy'}}], 'edges': []}
    result = score_saved(graph, [{'status': 'success', 'node_outputs': {'hidden': {'value': 1}}}], 'quality')
    assert result['metrics']['score'] == 1


@pytest.mark.parametrize('value', ['float("nan")', 'True', '"high"'])
def test_invalid_metric_values_cannot_become_objectives(monkeypatch, value):
    monkeypatch.setattr(chat_control, 'worker', lambda kind, payload, **kw: execute(payload))
    cfg = {'type': 'python', 'code': 'def evaluate(records):\n    return {"metrics": {"score": '+value+'}}'}
    with pytest.raises((ValueError, evaluators.SourceError)):
        evaluators.report(cfg, [])

CLASS_CODE = '''def helper(value, threshold):
    return value >= threshold
class AccuracyEvaluator:
    def __init__(self, threshold, labels):
        self.threshold, self.labels = threshold, labels
    def evaluate(self, records):
        return {"metrics": {"accuracy": sum(helper(r["value"], self.threshold) for r in records) / len(records)}, "details": self.labels}
def unrelated(hidden: str):
    raise RuntimeError("Do not call helpers automatically")
def build_evaluator(threshold: float = 0.5, label_records=None):
    return AccuracyEvaluator(threshold, label_records)
'''


def test_class_factory_interface_and_real_worker():
    schema = interface(CLASS_CODE)
    assert schema['entrypoint'] == 'build_evaluator'
    assert [f['name'] for f in schema['inputs']] == ['threshold']
    output = chat_control.worker('evaluate_python', {'code': CLASS_CODE, 'records': [{'value': .2}, {'value': .8}], 'config': {'threshold': .7, 'label_records': [{'id': 1}]}}, timeout=20)
    # The worker returns the report together with what the code printed.
    assert output['studio_evaluator_output'] and output['logs'] == ''
    result = output['report']
    assert result == {'metrics': {'accuracy': .5}, 'details': [{'id': 1}]}


@pytest.mark.parametrize('code, message', [
    ('def build_evaluator():\n return None', 'must return an object'),
    ('class E:\n def evaluate(self, records, missing): pass\ndef build_evaluator(): return E()', 'additional required'),
    ('async def build_evaluator(): pass', 'synchronous'),
    ('def build_evaluator(): pass\ndef build_evaluator(): pass', 'exactly once'),
    ('def build_evaluator(): pass\ndef evaluate(records): pass', 'Choose one'),
])
def test_invalid_class_contracts_fail_clearly(code, message):
    with pytest.raises(ValueError, match=message):
        execute({'code': code, 'records': [], 'config': {}})
