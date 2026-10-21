"""Conversations with Chat Agents (chat_agent nodes of the saved workflow)."""
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
from pydantic import BaseModel, Field, ConfigDict
from backend.api import graphs, harness, mem0_service, memory_resources
from backend.api.studio_config import data_path
from backend.features.agents import chat_agents
from backend.features.agents.chat_agents import MemoryBinding, space_of  # noqa: F401 - re-exported

router = APIRouter(prefix='/api/graphs/{graph_id}/agents', tags=['harness'])
# What Stop can and cannot reach: the Agent loop is interrupted at its next
# step, but a model or tool request the provider already accepted runs to its
# end. Said once, here, so the UI can show it verbatim.
STOP_NOTE = ('Stopping. The Agent stops at its next step; a model or tool request '
             'already sent cannot be recalled and finishes on the server.')
GONE_ERROR = 'Execution stopped. You can continue in this conversation.'
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='harness')
_lock = threading.RLock()
_running = {}


class AgentSettings(BaseModel):
    """A Chat Agent's settings, as the workflow assistant checks them before
    writing them into the node (agent_ops)."""
    model_config = ConfigDict(extra='forbid')
    name: str = Field(default='chat_agent', min_length=1, max_length=120)
    # "deepagents" is what agents saved before say; it is the same engine.
    engine: Literal['langgraph', 'deepagents'] = 'langgraph'
    description: str = Field(default='', max_length=2000)
    provider: str | None = None
    instructions: str = Field(default=chat_agents.DEFAULT_INSTRUCTIONS, max_length=30000)
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
    scope = str(graph.get('task_id') or graph['id'])
    legacy = str(graph['id'])
    if scope != legacy:
        # Older agents were stored under graph.id before project assignment
        # introduced task_id. Move definitions and sessions together, retaining
        # the old checkpoint namespace so conversation history still resumes.
        with _lock, database() as db:
            rows = db.execute('SELECT id, kind, parent, body FROM objects WHERE owner=?', (legacy,)).fetchall()
            for id, kind, parent, body in rows:
                value = json.loads(body)
                if kind == 'session': value.setdefault('checkpoint_scope', legacy)
                put(db, id, scope, kind, parent, value)
    return scope


def archive_owner(scopes, archived: str) -> int:
    """File a deleted workflow's Chat Agents and conversations under its
    archived id: kept, but a new workflow given the old id starts without
    them. Sessions keep the checkpoint namespace they were created under, so
    their history still resumes if the archive is ever restored."""
    scopes = [str(s) for s in dict.fromkeys(scopes) if s]
    moved = 0
    with _lock, database() as db:
        for scope in scopes:
            rows = db.execute('SELECT id, kind, parent, body FROM objects WHERE owner=?', (scope,)).fetchall()
            for id, kind, parent, body in rows:
                value = json.loads(body)
                if kind == 'session': value.setdefault('checkpoint_scope', scope)
                put(db, id, archived, kind, parent, value)
            moved += len(rows)
    return moved


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


def unavailable_memory(binding):
    """Why a Mem0 binding cannot be used here, naming the space and the fix."""
    space_id = space_of(binding)
    record = mem0_service.space_record(space_id)
    if record is None:
        return (f"Shared memory {space_id} no longer exists. Remove it under "
                "Connected memories, then save again.")
    return (f"Shared memory '{record.get('name') or space_id}' ({space_id}) belongs to another "
            "project or task, so this task cannot use it (the task may have been moved). "
            "Remove it under Connected memories, then save again.")


def validate_memories(graph, settings):
    ids = []
    for binding in settings['memories']:
        try: resource = memory_resources.resolve(graph, memory_resources.key(binding))
        except ValueError as exc:
            if space_of(binding): raise HTTPException(422, unavailable_memory(binding)) from exc
            raise HTTPException(422, str(exc)) from exc
        if binding.get('write') and not resource['writable']: raise HTTPException(422, resource['write_reason'])
        ids.append(resource['id'])
    if len(set(ids)) != len(ids): raise HTTPException(422, 'Duplicate memory connection')


UNSAVED = ('This Chat Agent is not in the saved workflow. Save the workflow to chat with it; '
           'a conversation uses the settings last saved.')


def agent_for(graph, agent_id):
    """The Chat Agent node `agent_id` of the saved workflow, as the harness
    runs it. The stored graph is the authority: a node only on the canvas
    (added or changed and not saved) is refused with what to do (409)."""
    task = chat_agents.find(graph, agent_id)
    if task is None: raise HTTPException(409, UNSAVED)
    try: return chat_agents.settings_of(task)
    except ValueError as exc: raise HTTPException(422, str(exc)) from exc


def active_sessions(graph) -> list:
    """Conversations of this workflow executing now."""
    scope = owner(graph)
    with _lock, database() as db:
        ids = [r[0] for r in db.execute("SELECT id FROM objects WHERE owner=? AND kind='session'", (scope,))]
        return [sid for sid in ids if sid in _running]


# --------------------------------------------------------------------------
# Migration: Chat Agents kept as harness rows before they were nodes
# --------------------------------------------------------------------------

MIGRATED = 'migrated-agent'
_migrate_lock = threading.Lock()


def _node_from_row(agent: dict, name: str) -> dict:
    settings = {k: agent[k] for k in chat_agents.SETTINGS if k in agent and k != 'description'}
    return chat_agents.normalize({'name': name, 'kind': chat_agents.KIND,
                                  'agent_id': agent['id'], **settings})


def migrate_agents(graph_id: str) -> int:
    """Turn this workflow's Chat Agent rows (kind 'agent') into chat_agent
    nodes of its saved graph; returns how many were added.

    Each active row becomes a node keeping its id as agent_id (so its
    conversations stay reachable), its position, settings and memories,
    named after it with a suffix when the name is taken. The graph is saved
    first and the rows marked 'migrated-agent' after (never deleted): an
    interruption in between is finished by the next call, which finds the
    node by agent_id and only marks the row. Removed Agents
    ('archived-agent') stay archived."""
    import logging
    with _migrate_lock:
        graph = graphs.load_graph(graph_id)
        if graph is None: return 0
        scope = owner(graph)
        with _lock, database() as db:
            rows = db.execute("SELECT id, body FROM objects WHERE owner=? AND kind='agent' ORDER BY rowid",
                              (scope,)).fetchall()
        if not rows: return 0
        tasks = list(graph.get('tasks') or [])
        present = {t.get('agent_id') for t in tasks if chat_agents.is_chat_agent(t)}
        taken = {t.get('name') for t in tasks}
        added, done = [], []
        for id, body in rows:
            agent = json.loads(body)
            if id in present:
                done.append((id, agent, chat_agents.find(graph, id)['name']))
                continue
            name = chat_agents.node_name(agent.get('name') or 'Chat Agent', taken)
            try: node = _node_from_row({**agent, 'id': id}, name)
            except ValueError as exc:
                logging.getLogger(__name__).warning('Chat Agent %s of %s not migrated: %s', id, graph_id, exc)
                continue
            taken.add(name); tasks.append(node); added.append(node)
            done.append((id, agent, name))
        if added:
            graphs.save_graph(graph['id'], {**graph, 'tasks': tasks})
        with _lock, database() as db:
            for id, agent, name in done:
                put(db, id, scope, MIGRATED, '', {**agent, 'migrated_to': {'graph_id': graph['id'], 'node': name}})
        return len(added)


def migrate_all() -> int:
    """migrate_agents for every workflow with rows left; for startup."""
    import logging
    with database() as db:
        owners = {r[0] for r in db.execute("SELECT DISTINCT owner FROM objects WHERE kind='agent'")}
    if not owners: return 0
    added = 0
    for summary in graphs.list_graphs():
        graph = graphs.load_graph(summary['id'])
        if not graph or not ({str(graph.get('task_id') or ''), str(graph['id'])} & owners): continue
        try: added += migrate_agents(graph['id'])
        except Exception as exc:  # noqa: BLE001 - one workflow must not stop the others
            logging.getLogger(__name__).warning('Chat Agents of %s not migrated: %s', graph['id'], exc)
    return added


@router.get('/memory-resources')
def list_memory_resources(graph_id: str):
    return {'resources': memory_resources.catalog(graph_for(graph_id))}


@router.get('/{agent_id}/sessions')
def list_sessions(graph_id: str, agent_id: str):
    # Sessions are listed for any agent_id of the workflow: a Chat Agent not
    # saved yet simply has none.
    scope = owner(graph_for(graph_id))
    with database() as db:
        rows = [json.loads(r[0]) for r in db.execute('SELECT body FROM objects WHERE owner=? AND kind=? AND parent=?', (scope, 'session', agent_id))]
    # Recover crashed executions; checkpoints remain untouched for diagnosis.
    with _lock:
        for row in rows:
            if row['id'] in _running and _running[row['id']].is_set():
                row.update(status='stopping', stop_note=STOP_NOTE)
            if row['status'] in ('running', 'stopping') and row['id'] not in _running:
                row.update(status='failed', stop_note=None,
                           error=row.get('error') or 'Server restarted during execution. You can continue in this conversation.')
    return {'sessions': sorted(rows, key=lambda s: s['created_at'], reverse=True)}


@router.post('/{agent_id}/sessions', status_code=201)
def create_session(graph_id: str, agent_id: str):
    graph = graph_for(graph_id)
    agent_for(graph, agent_id)
    scope = owner(graph)
    session = {'id': uuid.uuid4().hex, 'created_at': time.time(), 'status': 'idle', 'messages': [], 'events': [], 'error': None}
    with database() as db:
        put(db, session['id'], scope, 'session', agent_id, session)
    return session


def execution_error(exc):
    if isinstance(exc, ModuleNotFoundError):
        import sys
        return f"Missing Python module: {exc.name}. Install it in the backend environment ({sys.executable}), then send again."
    if isinstance(exc, ImportError):
        return 'Chat Agent dependency/interface mismatch. model_bridge.agent_model() must return a LangChain chat model that supports tool calling. Check backend package versions (langchain-core) and the configured provider in llm/providers.json.'
    if isinstance(exc, NotImplementedError):
        return 'The selected model adapter does not implement a capability required by Chat Agent, such as tool calling. Configure a compatible LangChain chat model.'
    text = str(exc).lower()
    if any(word in text for word in ('nodename', 'name resolution', 'disconnected', 'connection', 'timeout', 'timed out')):
        return 'Model service connection failed. Check the network/provider endpoint, then send again in this conversation.'
    return f'Execution failed ({type(exc).__name__}). You can send again in this conversation; server logs contain diagnostic details.'


def thread_id(scope, agent_id, session, generation=None):
    """The checkpoint thread of a session's execution generation. Sessions
    moved from an older owner keep the namespace they were created under."""
    generation = session.get('generation') if generation is None else generation
    base = f"{session.get('checkpoint_scope', scope)}:{agent_id}:{session['id']}"
    return hashlib.sha256((base + (f':recovery:{generation}' if generation else '')).encode()).hexdigest()


def _execute(graph, agent, session, event):
    scope, sid, aid = owner(graph), session['id'], agent['id']
    def persist():
        with database() as db: put(db, sid, scope, 'session', aid, session)
    def emit(item):
        if item.get('type') == 'token_usage':
            if item.get('content'):
                from backend.features.execution.token_usage import add_usage
                add_usage(session.setdefault('token_usage', {}), item['content'])
                add_usage(session.setdefault('turn_token_usage', {}), item['content'])
            else:
                session['usage_unavailable'] = True
        session['events'] = (session['events'] + [item])[-200:]
        persist()
    # The session's Stop is what every model call of this turn watches: a
    # call blocked in the provider is given up at once, not when it answers.
    from backend.features.chat import chat_control
    control = chat_control.Control()
    control.event = event
    token = chat_control.current.set(control)
    try:
        answer = harness.run_turn(graph, agent, thread_id(scope, aid, session), session['messages'][-1]['content'], emit, event)
        if event.is_set():
            session['status'] = 'stopped'
        else:
            session['messages'].append({'role': 'assistant', 'content': answer or '(No answer returned)'})
            session['status'] = 'idle'
    except chat_control.Cancelled:
        session['status'] = 'stopped'
        session['error'] = None
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception('Chat Agent execution failed')
        session['status'] = 'stopped' if event.is_set() else 'failed'
        # Provider errors may contain request headers. Do not expose raw exceptions.
        session['error'] = GONE_ERROR if event.is_set() else execution_error(exc)
    finally:
        chat_control.current.reset(token)
        session.pop('stop_note', None)     # it has settled; nothing is pending
        persist()
        with _lock: _running.pop(sid, None)


@router.post('/{agent_id}/sessions/{session_id}/messages', status_code=202)
def send_message(graph_id: str, agent_id: str, session_id: str, body: TurnInput):
    graph = graph_for(graph_id)
    if not body.message.strip(): raise HTTPException(422, 'Message must not be blank')
    agent = agent_for(graph, agent_id)
    with _lock, database() as db:
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
        except (ImportError, NotImplementedError) as exc: raise HTTPException(422, execution_error(exc)) from exc
        except Exception: raise HTTPException(422, 'Model configuration is unavailable or unsupported. Check the selected provider in llm/providers.json and its environment variables; Chat Agent needs a LangChain chat model with tool calling, in addition to workflow/batch support.')
        session['messages'].append({'role': 'user', 'content': body.message})
        session.update(status='running', error=None, events=[], turn_token_usage={}, usage_unavailable=False)
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
            session.update(status='stopping', stop_note=STOP_NOTE)
        elif session['status'] in ('running', 'stopping'):
            # Nothing is running it: the worker went with a restart or a crash.
            # Handing 'running' back is what left Stop disabled over a session
            # that was never going to settle.
            session.update(status='failed', stop_note=None, error=session.get('error') or GONE_ERROR)
        # Persisted, so the next reader agrees with what Stop just reported.
        put(db, session_id, scope, 'session', agent_id, session)
    return session


@router.delete('/{agent_id}/sessions/{session_id}')
def delete_session(graph_id: str, agent_id: str, session_id: str):
    scope = owner(graph_for(graph_id))
    with _lock, database() as db:
        session = get(db, session_id, scope, 'session', agent_id)
        if session_id in _running: raise HTTPException(409, 'Stop this conversation and wait for it to finish before deleting it.')
        from langgraph.checkpoint.sqlite import SqliteSaver
        path = harness.data_path('harness') / 'checkpoints.sqlite'
        if path.exists():
            with SqliteSaver.from_conn_string(str(path)) as saver:
                for generation in range(session.get('generation', 0) + 1):
                    saver.delete_thread(thread_id(scope, agent_id, session, generation))
        db.execute('DELETE FROM objects WHERE id=?', (session_id,))
    return {'removed': True}
