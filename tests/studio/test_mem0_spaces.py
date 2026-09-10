"""Shared Mem0 boundaries and real runner adapter, with no model calls."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from backend.api import mem0_service as service, memory_policy, runner


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(service, 'ROOT', tmp_path / 'mem0')
    db = Mock()
    monkeypatch.setattr(service, '_client', db)
    graph = {'id': 'a', 'project_id': 'p1'}
    other = {'id': 'b', 'project_id': 'p1'}
    foreign = {'id': 'c', 'project_id': 'p2'}
    space = service.create_space(graph, 'Shared')['id']
    return graph, other, foreign, space, db


def test_spaces_are_shared_within_project_only(setup):
    a, b, foreign, space, db = setup
    assert service.spaces(a) == service.spaces(b)
    assert service.spaces(foreign) == []
    with pytest.raises(ValueError): service.entries(foreign, space)
    db.get_all.assert_not_called()


def test_unassigned_tasks_are_isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(service, 'ROOT', tmp_path)
    a = {'id': 'a'}; b = {'id': 'b'}
    space = service.create_space(a, 'Private')['id']
    with pytest.raises(ValueError): service.get_space(b, space)


def test_writes_and_search_use_same_scope_without_inference(setup):
    a, b, _, space, db = setup
    db.search.return_value = {'results': []}
    service.add(a, space, 'keep this exactly')
    assert db.add.call_args.kwargs['infer'] is False
    assert db.add.call_args.kwargs['user_id'] == space
    service.entries(b, space, 'this', 4)
    assert db.search.call_args.kwargs['filters'] == {'user_id': space}
    assert db.search.call_args.kwargs['rerank'] is False


def test_mutation_cannot_use_id_from_other_space(setup):
    a, _, _, space, db = setup
    db.get.return_value = {'id': 'foreign', 'user_id': 'different-space'}
    with pytest.raises(ValueError): service.mutate(a, space, 'foreign', 'overwrite')
    with pytest.raises(ValueError): service.mutate(a, space, 'foreign')
    db.update.assert_not_called(); db.delete.assert_not_called()


def test_adapter_puts_shared_results_into_recall_block(setup):
    a, b, _, space, db = setup
    task = {'name': 'reader', 'description': 'summarize', 'use_long_term_memory': True,
            'memory': {'provider': 'mem0', 'space_id': space, 'write_enabled': False},
            'inputs': [{'name': 'topic'}]}
    db.search.return_value = {'results': [{'id': 'one', 'memory': json.dumps({'outputs': {'fact': 'Shared evidence'}})}]}
    state = {}
    memories, _ = runner._prepare_ltm(b, [task], {}, state)
    block = asyncio.run(memory_policy.recall_async(memories, task, {'topic': 'evidence'}))
    assert 'Shared evidence' in block
    assert state['memory_recalled'][0]['space_id'] == space
    db.add.assert_not_called()


def test_adapter_writes_raw_selected_payload(setup):
    a, _, _, space, db = setup
    task = {'name': 'writer', 'memory': {'provider': 'mem0', 'space_id': space}}
    state = {'run_id': 'run-test'}
    memory = service.Mem0Memory(a, task, state)
    payload = '{"outputs":{"fact":"evidence"}}'
    memory.add([SimpleNamespace(content=payload)])
    assert db.add.call_args.args == (payload,)
    assert state['memory_written'][0]['kind'] == 'mem0'


def test_api_scope_check_precedes_sdk_access(setup, monkeypatch):
    from fastapi.testclient import TestClient
    from backend.api.app import app
    from backend.api import graphs
    a, b, foreign, space, db = setup
    monkeypatch.setattr(graphs, 'graph_exists', lambda _id: True)
    monkeypatch.setattr(graphs, 'load_graph', lambda id: {'a': a, 'b': b, 'c': foreign}[id])
    api = TestClient(app)
    response = api.get(f'/api/graphs/c/mem0/spaces/{space}/entries')
    assert response.status_code == 422
    db.get_all.assert_not_called()
    db.get_all.return_value = {'results': []}
    assert api.get(f'/api/graphs/b/mem0/spaces/{space}/entries').status_code == 200
    response = api.post('/api/graphs/a/mem0/spaces', json={'name': ' '})
    assert response.status_code == 422


def test_runner_save_writes_to_selected_shared_space(setup):
    a, _, _, space, db = setup
    task = {'name': 'writer', 'use_long_term_memory': True,
            'memory': {'provider': 'mem0', 'space_id': space},
            'inputs': [{'name': 'topic'}], 'outputs': [{'name': 'fact'}]}
    graph = {**a, 'tasks': [task]}
    state = {'run_id': 'r1'}
    memories, _ = runner._prepare_ltm(graph, [task], {}, state)
    node = SimpleNamespace(name='writer', inputs=[SimpleNamespace(name='topic')], outputs=[SimpleNamespace(name='fact')])
    wf = SimpleNamespace(environment=SimpleNamespace(get_all_execution_data=lambda: {'topic': 'test', 'fact': 'actual output'}))
    runner._save_ltm(graph, memories, SimpleNamespace(nodes=[node]), wf, state)
    assert not state.get('memory_error'), state.get('memory_error')
    content = json.loads(db.add.call_args.args[0])
    assert content['outputs']['fact'] == 'actual output'
    assert db.add.call_args.kwargs['user_id'] == space


def test_foreign_space_binding_is_rejected_before_run(setup):
    from conftest import make_graph, make_task
    from backend.api import run_plan, graphs
    a, b, foreign, space, db = setup
    task = make_task('reader', inputs=['topic'], outputs=['answer'])
    task.update(use_long_term_memory=True, memory={'provider': 'mem0', 'space_id': space})
    graph = {**make_graph([task]), **foreign}
    with pytest.raises(graphs.GraphValidationError, match='different project'):
        run_plan.compile_plan(graph)
    db.search.assert_not_called()


def test_delete_space_checks_all_saved_connections_and_preserves_foreign_spaces(setup, monkeypatch):
    from backend.api import mem0_api, harness_api
    from fastapi import HTTPException
    a, b, foreign, space, db = setup
    monkeypatch.setattr(mem0_api, 'graph_for', lambda g: {'a': a, 'c': foreign}[g])
    monkeypatch.setattr(mem0_api.graphs, 'list_graphs', lambda: [b])
    monkeypatch.setattr(mem0_api.graphs, 'load_graph', lambda g: {**b, 'memory_resources': [{'space_id': space}]})
    monkeypatch.setattr(harness_api, 'list_agents', lambda g: {'agents': []})
    with pytest.raises(HTTPException) as error: mem0_api.delete_space('a', space)
    assert error.value.status_code == 409
    db.delete_all.assert_not_called()
    with pytest.raises(HTTPException): mem0_api.delete_space('c', space)
    monkeypatch.setattr(mem0_api.graphs, 'load_graph', lambda g: b)
    assert mem0_api.delete_space('a', space) == {'removed': True}
    db.delete_all.assert_called_once_with(user_id=space)
    assert service.spaces(a) == []
