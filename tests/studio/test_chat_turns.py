"""Assistant turns keep running on the server when the chat goes away."""
import threading
import time
import uuid

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.api import chat_api, chat_control
from backend.features.chat import turn_jobs


def _client():
    app = FastAPI()
    app.include_router(chat_api.router)
    return TestClient(app)


def _settle(client, graph_id, turn_id, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        turn = client.get(f'/api/graphs/{graph_id}/assistant/turns/{turn_id}').json()
        if turn['status'] != 'running':
            return turn
        time.sleep(.02)
    pytest.fail('turn did not settle')


def test_turn_continues_after_client_goes_away_and_result_is_visible(monkeypatch):
    release = threading.Event()
    seen = {}

    def slow_chat(graph_id, body):
        seen['control'] = chat_control.current.get()
        release.wait(5)
        return {'reply': f'Answer to {body["message"]}', 'operations': [], 'activity': []}

    monkeypatch.setattr(chat_api, 'chat', slow_chat)
    graph_id, turn_id = 'turn-' + uuid.uuid4().hex, uuid.uuid4().hex
    with _client() as client:
        started = client.post(f'/api/graphs/{graph_id}/assistant',
                              json={'message': 'hello', 'request_id': turn_id, 'background': True})
        assert started.status_code == 200
        assert started.json()['turn_id'] == turn_id
        assert started.json()['status'] == 'running'
        running = client.get(f'/api/graphs/{graph_id}/assistant/turns?context=canvas').json()['turns']
        assert [t['turn_id'] for t in running] == [turn_id]
    # The client that started the turn is gone; the work is not.
    release.set()
    with _client() as client:
        turn = _settle(client, graph_id, turn_id)
        assert turn['status'] == 'done'
        assert turn['result']['reply'] == 'Answer to hello'
        assert client.get(f'/api/graphs/{graph_id}/assistant/turns').json()['turns'] == []
        # Scoped to its workflow.
        assert client.get(f'/api/graphs/other/assistant/turns/{turn_id}').status_code == 404
    assert seen['control'] is not None and seen['control'].finished


def test_explicit_stop_cancels_a_background_turn(monkeypatch):
    def looping_chat(graph_id, body):
        for _ in range(500):
            chat_control.check()
            time.sleep(.01)
        return {'reply': 'should not finish'}

    monkeypatch.setattr(chat_api, 'chat', looping_chat)
    graph_id, turn_id = 'turn-' + uuid.uuid4().hex, uuid.uuid4().hex
    with _client() as client:
        client.post(f'/api/graphs/{graph_id}/assistant',
                    json={'message': 'slow', 'request_id': turn_id, 'background': True})
        assert client.post(f'/api/graphs/{graph_id}/assistant/{turn_id}/stop').status_code == 200
        turn = _settle(client, graph_id, turn_id)
    assert turn['status'] == 'cancelled'
    assert turn['result']['stopped'] is True


def test_failed_turn_reports_its_error_and_ids_are_single_use(monkeypatch):
    def refused(graph_id, body):
        raise HTTPException(422, 'message is required')

    monkeypatch.setattr(chat_api, 'chat', refused)
    graph_id, turn_id = 'turn-' + uuid.uuid4().hex, uuid.uuid4().hex
    with _client() as client:
        body = {'message': '', 'request_id': turn_id, 'background': True}
        client.post(f'/api/graphs/{graph_id}/assistant', json=body)
        turn = _settle(client, graph_id, turn_id)
        assert turn['status'] == 'failed'
        assert turn['error'] == 'message is required'
        assert turn['status_code'] == 422
        assert client.post(f'/api/graphs/{graph_id}/assistant', json=body).status_code == 409


def test_results_context_runs_in_the_background(monkeypatch):
    from backend.api import result_chat
    monkeypatch.setattr(result_chat, 'chat_results', lambda graph_id, body: {'reply': 'twelve', 'activity': [], 'count': 3})
    graph_id, turn_id = 'turn-' + uuid.uuid4().hex, uuid.uuid4().hex
    with _client() as client:
        started = client.post(f'/api/graphs/{graph_id}/assistant',
                              json={'context': 'results', 'message': 'count?', 'request_id': turn_id, 'background': True})
        assert started.json()['context'] == 'results'
        turn = _settle(client, graph_id, turn_id)
    assert turn['result'] == {'reply': 'twelve', 'activity': [], 'count': 3}


def test_inline_requests_are_unchanged(monkeypatch):
    monkeypatch.setattr(chat_api, 'chat', lambda graph_id, body: {'reply': 'inline'})
    assert chat_api.assistant('g', {'message': 'hi', 'request_id': uuid.uuid4().hex}) == {'reply': 'inline'}
    assert turn_jobs.running('g') == []


def test_harness_turn_finishes_after_the_client_goes_away(tmp_path, monkeypatch):
    from backend.api import harness_api as api
    monkeypatch.setattr(api, 'data_path', lambda *p: tmp_path.joinpath(*p))
    monkeypatch.setattr(api.graphs, 'load_graph', lambda id: {'id': id} if id == 'a' else None)
    monkeypatch.setattr(api.harness, 'make_model', lambda provider: object())
    release = threading.Event()

    def run_turn(graph, agent, thread_id, message, emit, event):
        release.wait(5)
        return f'Done: {message}'

    monkeypatch.setattr(api.harness, 'run_turn', run_turn)
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        agent = client.post('/api/graphs/a/agents', json={}).json()
        path = f'/api/graphs/a/agents/{agent["id"]}/sessions'
        session = client.post(path).json()
        assert client.post(f'{path}/{session["id"]}/messages', json={'message': 'research'}).status_code == 202
    release.set()
    with TestClient(app) as client:
        for _ in range(250):
            row = client.get(path).json()['sessions'][0]
            if row['status'] != 'running':
                break
            time.sleep(.02)
    assert row['status'] == 'idle'
    assert row['messages'][-1] == {'role': 'assistant', 'content': 'Done: research'}
