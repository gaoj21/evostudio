"""Local store identity survives display/node renames; legacy names still work."""
from contextvars import ContextVar
from functools import wraps

_snapshot = ContextVar('memory_graph_snapshot', default=None)


def execution_snapshot(function):
    @wraps(function)
    def run(*args, **kwargs):
        graph = args[1] if len(args) > 1 else kwargs.get('graph_doc', kwargs.get('graph'))
        token = _snapshot.set(graph)
        try:
            return function(*args, **kwargs)
        finally:
            _snapshot.reset(token)
    return run


def _graph(graph_id):
    snapshot = _snapshot.get()
    if snapshot is not None and snapshot.get('id') == graph_id:
        return snapshot
    from backend.api import graphs
    return graphs.load_graph(graph_id) or {}



def valid(value):
    return isinstance(value, str) and bool(value.strip()) and value not in ('.', '..') and not any(c in value for c in ('/', '\\', '\x00'))


def storage_name(graph_id, node):
    graph = _graph(graph_id)
    task = next((t for t in graph.get('tasks', []) if t.get('name') == node), {})
    identity = (task.get('memory') or {}).get('store_id')
    return identity if valid(identity) else node


def display_names(graph_id, names, normalize=lambda value: value):
    graph = _graph(graph_id)
    aliases = {normalize((t.get('memory') or {}).get('store_id')): t.get('name') for t in graph.get('tasks', []) if valid((t.get('memory') or {}).get('store_id'))}
    return sorted(aliases.get(n, n) for n in names)


def rename_references(graph, old, new):
    """Update qualified metadata, read links and store identity for Chat edits."""
    for task in graph.get('tasks', []):
        memory = task.get('memory')
        if task.get('name') == new and (task.get('use_long_term_memory') or memory is not None):
            memory = task.setdefault('memory', {}) or {}
            task['memory'] = memory
            memory.setdefault('store_id', old)
        if not memory:
            continue
        for key in ('at', 'match', 'key'):
            value = memory.get(key)
            if isinstance(value, str) and value.startswith(f'nodes.{old}.'):
                memory[key] = f'nodes.{new}.' + value[len(f'nodes.{old}.'):]
        if memory.get('context'):
            memory['context'] = [f'nodes.{new}.' + v[len(f'nodes.{old}.'):] if v.startswith(f'nodes.{old}.') else v for v in memory['context']]
        if memory.get('read_from'):
            memory['read_from'] = [new if n == old else n for n in memory['read_from']]
