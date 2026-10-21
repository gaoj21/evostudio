import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.api import harness_api as api

AGENT = 'a1' * 16
GRAPHS = {}


def node(**settings):
    return {'name': 'helper', 'kind': 'chat_agent', 'agent_id': AGENT, **settings}


def set_agent(graph_id, **settings):
    """The Chat Agent node of a saved workflow, as the harness reads it."""
    GRAPHS[graph_id] = {'id': graph_id, 'tasks': [node(**settings)]}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api, 'data_path', lambda *p: tmp_path.joinpath(*p))
    GRAPHS.clear()
    GRAPHS.update({'a': {'id': 'a', 'tasks': [node()]}, 'b': {'id': 'b', 'tasks': []}})
    monkeypatch.setattr(api.graphs, 'load_graph', lambda id: GRAPHS.get(id))
    monkeypatch.setattr(api.mem0_service, 'spaces', lambda g: [{'id': 'a' * 32, 'name': 'Knowledge'}] if g['id'] == 'a' else [])
    monkeypatch.setattr(api.harness, 'make_model', lambda provider: object())
    api._running.clear()
    app = FastAPI(); app.include_router(api.router)
    yield TestClient(app)
    api._running.clear()


def create(client):
    """The workflow's saved Chat Agent (the fixture's node)."""
    return {'id': AGENT}


def test_sessions_belong_to_a_saved_chat_agent_node(client):
    root = f'/api/graphs/a/agents/{AGENT}/sessions'
    session = client.post(root).json()
    assert client.get(root).json()['sessions'][0]['id'] == session['id']
    # Another workflow (another harness scope) has no such Agent saved.
    unsaved = client.post(f'/api/graphs/b/agents/{AGENT}/sessions')
    assert unsaved.status_code == 409 and 'Save the workflow' in unsaved.json()['detail']
    assert client.get(f'/api/graphs/b/agents/{AGENT}/sessions').json() == {'sessions': []}
    assert client.post('/api/graphs/missing/agents/x/sessions').status_code == 404
    # The old definition endpoints are gone: definitions are graph nodes.
    assert client.post('/api/graphs/a/agents', json={}).status_code in (404, 405)
    assert client.get('/api/graphs/a/agents').status_code in (404, 405)


def test_a_message_runs_with_the_saved_node_settings(client, monkeypatch):
    submitted = []
    monkeypatch.setattr(api._pool, 'submit', lambda *args: submitted.append(args))
    set_agent('a', instructions='Be terse.', max_steps=7, tools=['double'],
              memories=[{'space_id': 'a' * 32, 'read': True, 'write': False}])
    root = f'/api/graphs/a/agents/{AGENT}/sessions'
    session = client.post(root).json()
    assert client.post(f'{root}/{session["id"]}/messages', json={'message': 'hi'}).status_code == 202
    agent = submitted[-1][2]
    assert agent['id'] == AGENT and agent['name'] == 'helper'
    assert agent['instructions'] == 'Be terse.' and agent['max_steps'] == 7 and agent['tools'] == ['double']
    assert agent['memories'] == [{'space_id': 'a' * 32, 'read': True, 'write': False}]


def test_memories_out_of_scope_and_invalid_settings_refuse_the_turn(client, monkeypatch):
    monkeypatch.setattr(api._pool, 'submit', lambda *args: None)
    root = f'/api/graphs/a/agents/{AGENT}/sessions'
    session = client.post(root).json()
    turn = f'{root}/{session["id"]}/messages'
    set_agent('a', memories=[{'space_id': 'b' * 32}])
    assert client.post(turn, json={'message': 'hi'}).status_code == 422
    set_agent('a', max_steps=0)
    assert client.post(turn, json={'message': 'hi'}).status_code == 422
    set_agent('a')
    assert client.post(turn, json={'message': 'hi'}).status_code == 202


def test_execution_busy_stop_and_failure_recovery(client, monkeypatch):
    submitted = []
    monkeypatch.setattr(api._pool, 'submit', lambda *args: submitted.append(args))
    agent = create(client)
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
    agent = create(client)
    path = f'/api/graphs/a/agents/{agent["id"]}/sessions'
    session = client.post(path).json()
    def fail(_): raise ValueError('secret key should never reach UI')
    monkeypatch.setattr(api.harness, 'make_model', fail)
    response = client.post(f'{path}/{session["id"]}/messages', json={'message': 'hello'})
    assert response.status_code == 422
    assert 'secret key' not in response.text
    assert client.get(path).json()['sessions'][0]['messages'] == []

def test_deleting_the_node_keeps_its_conversations_for_an_undo(client):
    path = f'/api/graphs/a/agents/{AGENT}/sessions'
    session = client.post(path).json()
    GRAPHS['a'] = {'id': 'a', 'tasks': []}            # the node deleted and saved
    assert client.post(path).status_code == 409
    with api.database() as db:
        assert api.get(db, session['id'], 'a', 'session', AGENT)['id'] == session['id']
    set_agent('a')                                    # undo + save: the same agent_id
    assert [s['id'] for s in client.get(path).json()['sessions']] == [session['id']]


def test_active_sessions_are_reported_per_workflow(client):
    import threading
    session = client.post(f'/api/graphs/a/agents/{AGENT}/sessions').json()
    api._running[session['id']] = threading.Event()
    assert api.active_sessions(GRAPHS['a']) == [session['id']]
    assert api.active_sessions(GRAPHS['b']) == []


def test_legacy_resource_catalog_and_access_are_shared_with_chat(client, monkeypatch):
    monkeypatch.setattr(api._pool, 'submit', lambda *args: None)
    investigate = {'name': 'investigate', 'use_long_term_memory': True, 'memory': {'match': 'company'}}
    GRAPHS['a']['tasks'].append(investigate)
    catalog = client.get('/api/graphs/a/agents/memory-resources').json()['resources']
    resource = next(r for r in catalog if r['id'] == 'mem:investigate')
    assert resource['name'] == 'investigate memory'
    read = {'memory_id': resource['id'], 'read': True, 'write': False}
    root = f'/api/graphs/a/agents/{AGENT}/sessions'
    turn = f'{root}/{client.post(root).json()["id"]}/messages'
    GRAPHS['a']['tasks'][0]['memories'] = [{**read, 'write': True}]
    assert client.post(turn, json={'message': 'hi'}).status_code == 422
    GRAPHS['a']['tasks'][0]['memories'] = [read]
    assert client.post(turn, json={'message': 'hi'}).status_code == 202


def test_delete_conversation_removes_all_checkpoints_and_enforces_scope(client, monkeypatch, tmp_path):
    import hashlib, threading
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.checkpoint.base import empty_checkpoint
    monkeypatch.setattr(api.harness, 'data_path', lambda *p: tmp_path.joinpath(*p))
    agent = create(client)
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


def test_legacy_owner_migration_preserves_session_and_checkpoint_scope(client):
    root = f'/api/graphs/a/agents/{AGENT}/sessions'
    session = client.post(root).json()
    GRAPHS['a'] = {**GRAPHS['a'], 'task_id': 'stable-task'}
    restored = client.get(root).json()['sessions'][0]
    assert restored['id'] == session['id']
    assert restored['checkpoint_scope'] == 'a'
    with api.database() as db:
        assert db.execute('SELECT count(*) FROM objects WHERE owner=?', ('a',)).fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM objects WHERE owner=?', ('stable-task',)).fetchone()[0] == 1


def test_delete_conversation_removes_checkpoints_of_a_migrated_session(client, monkeypatch, tmp_path):
    """A session moved from an older owner keeps its checkpoint namespace;
    deleting it must delete those checkpoints, not the new owner's."""
    import hashlib
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.checkpoint.base import empty_checkpoint
    monkeypatch.setattr(api.harness, 'data_path', lambda *p: tmp_path.joinpath(*p))
    root = '/api/graphs/a/agents'
    agent = create(client)
    session = client.post(f'{root}/{agent["id"]}/sessions').json()
    GRAPHS['a'] = {**GRAPHS['a'], 'task_id': 'stable-task'}
    assert client.get(f'{root}/{agent["id"]}/sessions').json()['sessions'][0]['checkpoint_scope'] == 'a'
    legacy = hashlib.sha256(f'a:{agent["id"]}:{session["id"]}'.encode()).hexdigest()
    assert api.thread_id('stable-task', agent['id'], {**session, 'checkpoint_scope': 'a'}) == legacy
    checkpoint_path = tmp_path / 'harness' / 'checkpoints.sqlite'
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    with SqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
        saver.put({'configurable': {'thread_id': legacy, 'checkpoint_ns': ''}}, empty_checkpoint(), {}, {})
    assert client.delete(f'{root}/{agent["id"]}/sessions/{session["id"]}').status_code == 200
    with SqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
        assert saver.get_tuple({'configurable': {'thread_id': legacy}}) is None
