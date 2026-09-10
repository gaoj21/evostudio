"""Durable, independently runnable canvas Agents and chat sessions."""
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Literal
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ConfigDict, model_validator
from backend.api import graphs, harness, mem0_service, memory_resources
from backend.api.studio_config import data_path

router = APIRouter(prefix='/api/graphs/{graph_id}/agents', tags=['harness'])
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='harness')
_lock = threading.RLock()
_running = {}


class MemoryBinding(BaseModel):
    space_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')
    memory_id: str | None = Field(default=None, min_length=5, max_length=200)

    @model_validator(mode='after')
    def reference(self):
        if bool(self.space_id) == bool(self.memory_id): raise ValueError('Provide exactly one memory reference')
        return self
    read_source_handle: str | None = Field(default=None, max_length=40)
    read_target_handle: str | None = Field(default=None, max_length=40)
    write_source_handle: str | None = Field(default=None, max_length=40)
    write_target_handle: str | None = Field(default=None, max_length=40)
    read: bool = True
    write: bool = False


class AgentSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(default='Chat Agent', min_length=1, max_length=120)
    engine: Literal['deepagents'] = 'deepagents'
    provider: str | None = None
    instructions: str = Field(default='You are a helpful research assistant.', max_length=30000)
    max_steps: int = Field(default=20, ge=1, le=100)
    timeout: int = Field(default=300, ge=10, le=1800)
    toolkits: list[str] | None = None
    tools: list[str] | None = None  # None = all; [] = no platform tools.
    skill_names: list[str] = Field(default_factory=list, max_length=100)
    memories: list[MemoryBinding] = Field(default_factory=list, max_length=100)
    x: float = Field(default=400, allow_inf_nan=False)
    y: float = Field(default=250, allow_inf_nan=False)


class TurnInput(BaseModel):
    message: str = Field(min_length=1, max_length=30000)


def graph_for(graph_id):
    graph = graphs.load_graph(graph_id)
    if graph is None: raise HTTPException(404, 'Task not found')
    return graph


def owner(graph):
    return str(graph.get('task_id') or graph['id'])


@contextmanager
def database():
    path = data_path('harness') / 'studio.sqlite'
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=30) as db:
        db.execute('CREATE TABLE IF NOT EXISTS objects (id TEXT PRIMARY KEY, owner TEXT, kind TEXT, parent TEXT, body TEXT)')
        yield db


def put(db, id, scope, kind, parent, body):
    db.execute('INSERT OR REPLACE INTO objects VALUES (?, ?, ?, ?, ?)', (id, scope, kind, parent, json.dumps(body)))


def get(db, id, scope, kind, parent=None):
    row = db.execute('SELECT parent, body FROM objects WHERE id=? AND owner=? AND kind=?', (id, scope, kind)).fetchone()
    if not row or (parent is not None and row[0] != parent): raise HTTPException(404, f'{kind.title()} not found')
    return json.loads(row[1])


def validate_memories(graph, settings):
    ids = []
    for binding in settings['memories']:
        try: resource = memory_resources.resolve(graph, memory_resources.key(binding))
        except ValueError as exc: raise HTTPException(422, str(exc)) from exc
        if binding.get('write') and not resource['writable']: raise HTTPException(422, resource['write_reason'])
        ids.append(resource['id'])
    if len(set(ids)) != len(ids): raise HTTPException(422, 'Duplicate memory connection')


@router.get('/memory-resources')
def list_memory_resources(graph_id: str):
    return {'resources': memory_resources.catalog(graph_for(graph_id))}


@router.get('')
def list_agents(graph_id: str):
    scope = owner(graph_for(graph_id))
    with database() as db:
        return {'agents': [json.loads(r[0]) for r in db.execute('SELECT body FROM objects WHERE owner=? AND kind=?', (scope, 'agent'))]}


@router.post('', status_code=201)
def create_agent(graph_id: str, body: AgentSettings):
    graph = graph_for(graph_id)
    settings = body.model_dump()
    validate_memories(graph, settings)
    agent = {'id': uuid.uuid4().hex, **settings}
    with database() as db: put(db, agent['id'], owner(graph), 'agent', '', agent)
    return agent


@router.put('/{agent_id}')
def update_agent(graph_id: str, agent_id: str, body: AgentSettings):
    graph = graph_for(graph_id)
    settings = body.model_dump()
    validate_memories(graph, settings)
    with database() as db:
        get(db, agent_id, owner(graph), 'agent')
        agent = {'id': agent_id, **settings}
        put(db, agent_id, owner(graph), 'agent', '', agent)
    return agent


@router.get('/{agent_id}/sessions')
def list_sessions(graph_id: str, agent_id: str):
    scope = owner(graph_for(graph_id))
    with database() as db:
        get(db, agent_id, scope, 'agent')
        rows = [json.loads(r[0]) for r in db.execute('SELECT body FROM objects WHERE owner=? AND kind=? AND parent=?', (scope, 'session', agent_id))]
    # Recover crashed executions; checkpoints remain untouched for diagnosis.
    with _lock:
        for row in rows:
            if row['id'] in _running and _running[row['id']].is_set():
                row['status'] = 'stopping'
            if row['status'] in ('running', 'stopping') and row['id'] not in _running:
                row.update(status='failed', error='Server restarted during execution. You can continue in this conversation.')
    return {'sessions': sorted(rows, key=lambda s: s['created_at'], reverse=True)}


@router.post('/{agent_id}/sessions', status_code=201)
def create_session(graph_id: str, agent_id: str):
    scope = owner(graph_for(graph_id))
    session = {'id': uuid.uuid4().hex, 'created_at': time.time(), 'status': 'idle', 'messages': [], 'events': [], 'error': None}
    with database() as db:
        get(db, agent_id, scope, 'agent')
        put(db, session['id'], scope, 'session', agent_id, session)
    return session


def execution_error(exc):
    text = str(exc).lower()
    if any(word in text for word in ('nodename', 'name resolution', 'disconnected', 'connection', 'timeout', 'timed out')):
        return 'Model service connection failed. Check the network/provider endpoint, then send again in this conversation.'
    return f'Execution failed ({type(exc).__name__}). You can send again in this conversation; server logs contain diagnostic details.'


def _execute(graph, agent, session, event):
    scope, sid, aid = owner(graph), session['id'], agent['id']
    def persist():
        with database() as db: put(db, sid, scope, 'session', aid, session)
    def emit(item):
        session['events'] = (session['events'] + [item])[-200:]
        persist()
    try:
        thread_id = hashlib.sha256((f"{scope}:{aid}:{sid}" + (f":recovery:{session['generation']}" if session.get('generation') else '')).encode()).hexdigest()
        answer = harness.run_turn(graph, agent, thread_id, session['messages'][-1]['content'], emit, event)
        if event.is_set():
            session['status'] = 'stopped'
        else:
            session['messages'].append({'role': 'assistant', 'content': answer or '(No answer returned)'})
            session['status'] = 'idle'
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception('Deep Agent execution failed')
        session['status'] = 'stopped' if event.is_set() else 'failed'
        # Provider errors may contain request headers. Do not expose raw exceptions.
        session['error'] = 'Execution stopped. You can continue in this conversation.' if event.is_set() else execution_error(exc)
    finally:
        persist()
        with _lock: _running.pop(sid, None)


@router.post('/{agent_id}/sessions/{session_id}/messages', status_code=202)
def send_message(graph_id: str, agent_id: str, session_id: str, body: TurnInput):
    graph = graph_for(graph_id)
    if not body.message.strip(): raise HTTPException(422, 'Message must not be blank')
    with _lock, database() as db:
        agent = get(db, agent_id, owner(graph), 'agent')
        session = get(db, session_id, owner(graph), 'session', agent_id)
        if session_id in _running: raise HTTPException(409, 'This session is already running')
        if session['status'] != 'idle':
            # New execution state, same visible conversation. Never replay pending tools.
            session['generation'] = session.get('generation', 0) + 1
            if session['messages'] and session['messages'][-1]['role'] == 'user':
                session['messages'][-1]['failed'] = True
            agent = {**agent, '_recovery_history': [
                {'role': m['role'], 'content': m['content']} for m in session['messages'] if not m.get('failed')]}

        if len(_running) >= 4: raise HTTPException(429, 'Four Agents are already running; try again when one finishes')
        validate_memories(graph, agent)
        # Validate credentials/adapters before accepting the turn. No model call.
        try: harness.make_model(agent.get('provider'))
        except Exception: raise HTTPException(422, 'Model configuration is unavailable or unsupported. Check the selected provider.')
        session['messages'].append({'role': 'user', 'content': body.message})
        session.update(status='running', error=None, events=[])
        put(db, session_id, owner(graph), 'session', agent_id, session)
        db.commit()
        event = threading.Event()
        _running[session_id] = event
        _pool.submit(_execute, graph, agent, session, event)
    return session


@router.post('/{agent_id}/sessions/{session_id}/stop')
def stop_session(graph_id: str, agent_id: str, session_id: str):
    scope = owner(graph_for(graph_id))
    with _lock, database() as db:
        session = get(db, session_id, scope, 'session', agent_id)
        if session_id in _running:
            _running[session_id].set()
            session['status'] = 'stopping'
    return session


@router.delete('/{agent_id}')
def remove_agent(graph_id: str, agent_id: str):
    """Archive the canvas Agent without erasing sessions or checkpoints."""
    scope = owner(graph_for(graph_id))
    with _lock, database() as db:
        agent = get(db, agent_id, scope, 'agent')
        sessions = [r[0] for r in db.execute('SELECT id FROM objects WHERE owner=? AND kind=? AND parent=?', (scope, 'session', agent_id))]
        if any(sid in _running for sid in sessions):
            raise HTTPException(409, 'Stop this Agent and wait for execution to finish before removing it')
        put(db, agent_id, scope, 'archived-agent', '', agent)
    return {'removed': True, 'history_retained': True}


@router.delete('/{agent_id}/sessions/{session_id}')
def delete_session(graph_id: str, agent_id: str, session_id: str):
    scope = owner(graph_for(graph_id))
    with _lock, database() as db:
        get(db, agent_id, scope, 'agent')
        session = get(db, session_id, scope, 'session', agent_id)
        if session_id in _running: raise HTTPException(409, 'Stop this conversation and wait for it to finish before deleting it.')
        from langgraph.checkpoint.sqlite import SqliteSaver
        path = harness.data_path('harness') / 'checkpoints.sqlite'
        if path.exists():
            with SqliteSaver.from_conn_string(str(path)) as saver:
                for generation in range(session.get('generation', 0) + 1):
                    suffix = f':recovery:{generation}' if generation else ''
                    thread = hashlib.sha256(f'{scope}:{agent_id}:{session_id}{suffix}'.encode()).hexdigest()
                    saver.delete_thread(thread)
        db.execute('DELETE FROM objects WHERE id=?', (session_id,))
    return {'removed': True}
