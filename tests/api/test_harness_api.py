import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.api import harness_api as api

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api, 'data_path', lambda *p: tmp_path.joinpath(*p))
    monkeypatch.setattr(api.graphs, 'load_graph', lambda id: {'id': id} if id in ['a', 'b'] else None)
    monkeypatch.setattr(api.mem0_service, 'spaces', lambda g: [{'id': 'a' * 32, 'name': 'Knowledge'}] if g['id'] == 'a' else [])
    monkeypatch.setattr(api.harness, 'make_model', lambda provider: object())
    api._running.clear()
    app = FastAPI(); app.include_router(api.router)
    yield TestClient(app)
    api._running.clear()

def test_agent_settings_scope_and_sessions(client):
    root = '/api/graphs/a/agents'
    agent = client.post(root, json={}).json()
    assert client.get(root).json()['agents'][0]['engine'] == 'deepagents'
    assert client.get('/api/graphs/b/agents').json() == {'agents': []}
    assert client.post(f'/api/graphs/b/agents/{agent["id"]}/sessions').status_code == 404
    assert client.post(root, json={'memories': [{'space_id': 'b' * 32}]}).status_code == 422
    assert client.post(root, json={'max_steps': 0}).status_code == 422
    path = f'{root}/{agent["id"]}'
    session = client.post(path + '/sessions').json()
    assert client.get(path + '/sessions').json()['sessions'][0]['id'] == session['id']
    settings = {k: v for k, v in agent.items() if k != 'id'}
    result = client.put(path, json={**settings, 'x': 90, 'memories': [{'space_id': 'a' * 32, 'read': True, 'write': False}]})
    assert result.status_code == 200
    assert result.json()['x'] == 90

def test_execution_busy_stop_and_failure_recovery(client, monkeypatch):
    submitted = []
    monkeypatch.setattr(api._pool, 'submit', lambda *args: submitted.append(args))
    agent = client.post('/api/graphs/a/agents', json={}).json()
    path = f'/api/graphs/a/agents/{agent["id"]}/sessions'
    session = client.post(path).json()
    turn = f'{path}/{session["id"]}'
    assert client.post(turn + '/messages', json={'message': 'hello'}).status_code == 202
    assert client.post(turn + '/messages', json={'message': 'again'}).status_code == 409
    assert client.post(turn + '/stop').json()['status'] == 'stopping'
    assert api._running[session['id']].is_set()
    monkeypatch.setattr(api.harness, 'run_turn', lambda *args: 'finished')
    fn, *args = submitted[0]; fn(*args)
    restored = client.get(path).json()['sessions'][0]
    assert restored['status'] == 'stopped'
    assert len(restored['messages']) == 1
    assert client.post(turn + '/messages', json={'message': 'again'}).status_code == 202
    assert submitted[-1][2]['_recovery_history'] == []
    assert submitted[-1][3]['generation'] == 1
    fn, *args = submitted[-1]; fn(*args)
    assert client.get(path).json()['sessions'][0]['status'] == 'idle'
    assert client.post(turn + '/messages', json={'message': 'follow up'}).status_code == 202
    assert '_recovery_history' not in submitted[-1][2]

def test_no_turn_accepted_with_invalid_model(client, monkeypatch):
    agent = client.post('/api/graphs/a/agents', json={}).json()
    path = f'/api/graphs/a/agents/{agent["id"]}/sessions'
    session = client.post(path).json()
    def fail(_): raise ValueError('secret key should never reach UI')
    monkeypatch.setattr(api.harness, 'make_model', fail)
    response = client.post(f'{path}/{session["id"]}/messages', json={'message': 'hello'})
    assert response.status_code == 422
    assert 'secret key' not in response.text
    assert client.get(path).json()['sessions'][0]['messages'] == []

def test_remove_archives_agent_and_preserves_conversation(client):
    agent = client.post('/api/graphs/a/agents', json={}).json()
    path = f'/api/graphs/a/agents/{agent["id"]}'
    session = client.post(path + '/sessions').json()
    assert client.delete(f'/api/graphs/b/agents/{agent["id"]}').status_code == 404
    assert client.delete(path).json() == {'removed': True, 'history_retained': True}
    assert client.get('/api/graphs/a/agents').json()['agents'] == []
    with api.database() as db:
        assert api.get(db, session['id'], 'a', 'session', agent['id'])['id'] == session['id']
    assert client.post(path + '/sessions').status_code == 404

def test_remove_running_agent_is_rejected(client):
    import threading
    agent = client.post('/api/graphs/a/agents', json={}).json()
    path = f'/api/graphs/a/agents/{agent["id"]}'
    session = client.post(path + '/sessions').json()
    api._running[session['id']] = threading.Event()
    assert client.delete(path).status_code == 409
    assert len(client.get('/api/graphs/a/agents').json()['agents']) == 1

def test_legacy_resource_catalog_and_access_are_shared_with_chat(client, monkeypatch):
    monkeypatch.setattr(api.graphs, 'load_graph', lambda id: {'id': id, 'tasks': [{'name': 'investigate', 'use_long_term_memory': True, 'memory': {'match': 'company'}}]} if id == 'a' else {'id': id, 'tasks': []})
    catalog = client.get('/api/graphs/a/agents/memory-resources').json()['resources']
    resource = next(r for r in catalog if r['id'] == 'mem:investigate')
    assert resource['name'] == 'investigate memory'
    read = {'memory_id': resource['id'], 'read': True, 'write': False}
    assert client.post('/api/graphs/a/agents', json={'memories': [read]}).status_code == 201
    assert client.post('/api/graphs/b/agents', json={'memories': [read]}).status_code == 422
    assert client.post('/api/graphs/a/agents', json={'memories': [{**read, 'write': True}]}).status_code == 422


def test_delete_conversation_removes_all_checkpoints_and_enforces_scope(client, monkeypatch, tmp_path):
    import hashlib, threading
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.checkpoint.base import empty_checkpoint
    monkeypatch.setattr(api.harness, 'data_path', lambda *p: tmp_path.joinpath(*p))
    agent = client.post('/api/graphs/a/agents', json={}).json()
    root = f'/api/graphs/a/agents/{agent["id"]}/sessions'
    session = client.post(root).json()
    path = f'{root}/{session["id"]}'
    session['generation'] = 1
    with api.database() as db: api.put(db, session['id'], 'a', 'session', agent['id'], session)
    ids = [hashlib.sha256(f'a:{agent["id"]}:{session["id"]}{suffix}'.encode()).hexdigest() for suffix in ['', ':recovery:1']]
    checkpoint_path = tmp_path / 'harness' / 'checkpoints.sqlite'
    with SqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
        for id in [*ids, 'unrelated']:
            saver.put({'configurable': {'thread_id': id, 'checkpoint_ns': ''}}, empty_checkpoint(), {}, {})
    assert client.delete(path.replace('/graphs/a/', '/graphs/b/')).status_code == 404
    api._running[session['id']] = threading.Event()
    assert client.delete(path).status_code == 409
    api._running.pop(session['id'])
    assert client.delete(path).status_code == 200
    assert client.get(root).json()['sessions'] == []
    assert client.delete(path).status_code == 404
    with SqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
        assert all(saver.get_tuple({'configurable': {'thread_id': id}}) is None for id in ids)
        assert saver.get_tuple({'configurable': {'thread_id': 'unrelated'}}) is not None


def test_tool_and_skill_settings_persist(client):
    root = '/api/graphs/a/agents'
    agent = client.post(root, json={}).json()
    assert agent['tools'] is None and agent['skill_names'] == []
    settings = {k:v for k,v in agent.items() if k != 'id'}
    res = client.put(root + '/' + agent['id'], json={**settings, 'tools':['double'], 'skill_names':['analysis']})
    assert res.status_code == 200
    loaded = client.get(root).json()['agents'][0]
    assert loaded['tools'] == ['double'] and loaded['skill_names'] == ['analysis']
