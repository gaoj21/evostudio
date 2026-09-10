"""Shared identity and access rules for canvas and chat Memory resources."""
from backend.api import mem0_service, memory_store, table_store


def key(binding):
    return binding.get('memory_id') or 'mem:space:' + binding['space_id']


def catalog(graph):
    names = {r['space_id']: r.get('name') for r in graph.get('memory_resources', []) if r.get('name')}
    result = [{'id': 'mem:space:' + s['id'], 'name': names.get(s['id']) or s['name'],
               'kind': 'mem0', 'space_id': s['id'], 'writable': True} for s in mem0_service.spaces(graph)]
    for task in graph.get('tasks', []):
        policy = task.get('memory') or {}
        if not task.get('use_long_term_memory') or policy.get('provider') == 'mem0': continue
        kind = policy.get('kind') or ('table' if policy.get('match') else 'recall')
        result.append({'id': 'mem:' + task['name'], 'name': task['name'] + ' memory', 'kind': kind,
                       'node': task['name'], 'writable': False,
                       'write_reason': 'Written by the owning workflow Agent; Chat can read this history.',
                       'subject_field': policy.get('match'), 'date_field': policy.get('at')})
    return result


def resolve(graph, reference):
    resources = catalog(graph)
    matches = [r for r in resources if reference in (r['id'], r.get('space_id'), r['name'])]
    if len(matches) != 1: raise ValueError('Memory is missing or its name is ambiguous. Use the resource ID from list_connected_memories.')
    return matches[0]


def read(graph, resource, query='', limit=10, before=None, offset=0):
    if resource['kind'] == 'mem0':
        return mem0_service.entries(graph, resource['space_id'], query or None, limit)
    if resource['kind'] == 'table':
        needle = query.strip().casefold()
        rows = [r for r in table_store.rows(graph['id'], resource['node'])
                if (not needle or needle in r['subject'].casefold() or r['subject'].casefold() in needle)
                and (not before or r['at'] < before)]
        rows.sort(key=lambda r: (r['at'], r['subject']), reverse=True)
        return [{'subject': r['subject'], 'at': r['at'], 'content': r['payload']} for r in rows[offset:offset + limit]]
    store = memory_store.open_memory(graph['id'], resource['node'], create=False)
    if store is None: return []
    if not query: return memory_store.list_entries(graph['id'], resource['node'])[:limit]
    from memory import unquote_content
    return [{'content': str(unquote_content(message.content)), 'score': float(score)} for message, score in store.search(query, n=limit)]


def preview(graph, resource, query, limit=5):
    """Recall a subject when recognized, otherwise browse without claiming an empty store."""
    hits = read(graph, resource, query, limit)
    if resource['kind'] != 'table':
        return {'records': hits, 'mode': 'search', 'returned': len(hits)}
    total = len(table_store.rows(graph['id'], resource['node']))
    mode = 'subject_search'
    if not hits:
        hits = read(graph, resource, '', limit)
        mode = 'browse'
    return {'records': hits, 'mode': mode, 'returned': len(hits), 'total_records': total,
            'note': 'Preview only. Use browse_memory pages for a complete overview; do not infer all companies from this sample.'}
