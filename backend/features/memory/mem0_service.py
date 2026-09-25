"""Project-scoped Mem0 OSS spaces. Raw writes; local embeddings by default."""
import asyncio
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from backend.api.studio_config import data_path

ROOT = data_path('mem0')
_lock = threading.RLock()
_client = None


def status():
    try:
        installed = version('mem0ai')
    except PackageNotFoundError:
        installed = None
    return {'provider': 'mem0', 'version': installed, 'available': installed is not None,
            'write_mode': 'raw', 'embedding': 'local HuggingFace',
            'storage': 'persistent local Qdrant', 'initialized': _client is not None}


def client():
    global _client
    with _lock:
        if _client is None:
            # No telemetry or model inference. Embeddings use the model already
            # used by this workspace; a missing local model fails explicitly.
            os.environ['MEM0_TELEMETRY'] = 'false'
            os.environ.setdefault('MEM0_DIR', str(ROOT))
            from mem0 import Memory
            from backend.memory.ltm import embedding_model
            ROOT.mkdir(parents=True, exist_ok=True)
            _client = Memory.from_config({
                'embedder': {'provider': 'huggingface', 'config': {
                    'model': embedding_model(), 'embedding_dims': 384,
                    'model_kwargs': {'device': 'cpu', 'local_files_only': True}}},
                'llm': {'provider': 'ollama', 'config': {'model': 'llama3.2'}},
                'vector_store': {'provider': 'qdrant', 'config': {
                    'path': str(ROOT / 'vectors'), 'collection_name': 'evoagentx_memories',
                    'embedding_model_dims': 384, 'on_disk': True}},
                'history_db_path': str(ROOT / 'history.db'),
            })
        return _client


def scope(graph):
    return 'project:' + graph['project_id'] if graph.get('project_id') else 'task:' + str(graph.get('task_id') or graph['id'])


def spaces(graph):
    result = []
    for path in (ROOT / 'spaces').glob('*.json'):
        item = json.loads(path.read_text())
        if item['scope'] == scope(graph):
            result.append(item)
    return sorted(result, key=lambda item: item['name'])


def get_space(graph, space_id):
    if not isinstance(space_id, str) or not re.fullmatch('[a-f0-9]{32}', space_id):
        raise ValueError('Select a Mem0 space accessible to this task.')
    path = ROOT / 'spaces' / (space_id + '.json')
    if not path.is_file():
        raise ValueError('Mem0 space not found.')
    item = json.loads(path.read_text())
    if item['scope'] != scope(graph):
        raise ValueError('Mem0 space belongs to a different project or task.')
    return item


def create_space(graph, name):
    with _lock:
        item = {'id': uuid.uuid4().hex, 'name': name, 'scope': scope(graph),
                'created_at': datetime.now(timezone.utc).isoformat()}
        folder = ROOT / 'spaces'
        folder.mkdir(parents=True, exist_ok=True)
        temp = folder / (item['id'] + '.tmp')
        temp.write_text(json.dumps(item, ensure_ascii=False))
        temp.replace(folder / (item['id'] + '.json'))
        return item


def _rows(response):
    return response.get('results', []) if isinstance(response, dict) else response


def entries(graph, space_id, query=None, limit=100):
    get_space(graph, space_id)
    with _lock:
        db = client()
        filters = {'user_id': space_id}
        response = db.search(query, filters=filters, top_k=limit, threshold=0, rerank=False) if query else db.get_all(filters=filters, top_k=limit)
        return _rows(response)


def add(graph, space_id, content, metadata=None):
    get_space(graph, space_id)
    with _lock:
        return client().add(content, user_id=space_id, infer=False, metadata={
            **(metadata or {}), 'graph_id': graph['id'], 'scope': scope(graph)})


def mutate(graph, space_id, memory_id, content=None):
    get_space(graph, space_id)
    with _lock:
        db = client()
        item = db.get(memory_id)
        if not item or item.get('user_id') != space_id:
            raise ValueError('Memory entry not found in this space.')
        return db.delete(memory_id) if content is None else db.update(memory_id, text=content)


class Mem0Memory:
    """Adapter for the existing runner; every task chooses a shared space."""
    storage_handler = None

    def __init__(self, graph, task, state=None):
        self.graph, self.task, self.state = graph, task, state
        self.space_id = (task.get('memory') or {}).get('space_id')
        get_space(graph, self.space_id)

    def add(self, messages):
        for message in messages:
            add(self.graph, self.space_id, str(message.content), {'node': self.task['name'],
                'run_id_source': (self.state or {}).get('run_id')})
        if self.state is not None:
            self.state.setdefault('memory_written', []).append({
                'node': self.task['name'], 'kind': 'mem0', 'space_id': self.space_id})

    def search(self, query, n=3):
        from evoagentx.core.message import Message
        rows = entries(self.graph, self.space_id, str(query), n)
        if self.state is not None:
            self.state.setdefault('memory_recalled', []).append({
                'node': self.task['name'], 'kind': 'mem0', 'space_id': self.space_id,
                'count': len(rows)})
        return [(Message(content=row['memory'], agent=(row.get('metadata') or {}).get('node')), row['id']) for row in rows]

    async def search_async(self, query, n=3):
        return await asyncio.to_thread(self.search, query, n)

    def save(self):
        pass  # Mem0 writes through to Qdrant and SQLite.
