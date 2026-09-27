"""A Chat Agent reading a vector memory before its turn: the search returns
(message, memory_id) — not a score — works where an event loop runs, and
leaves no store open."""
import asyncio
import os


def graph_with_vector_memory():
    return {'id': 'g-recall', 'tasks': [{'name': 'notes', 'use_long_term_memory': True,
                                          'memory': {'kind': 'recall'}}], 'memory_resources': []}


def seed(memory_store):
    from evoagentx.core.message import Message
    store = memory_store.open_memory('g-recall', 'notes', create=True)
    store.add([Message(content='Acme exchanged its senior notes in March', agent='notes'),
               Message(content='Globex opened a new plant', agent='notes')])
    store.save()
    memory_store.close_memory(store)


def test_a_vector_memory_preview_returns_its_hits(studio_data):
    from backend.features.memory import memory_resources, memory_store
    seed(memory_store)
    graph = graph_with_vector_memory()
    resource = memory_resources.resolve(graph, 'mem:notes')
    before = len(os.listdir('/dev/fd'))

    preview = memory_resources.preview(graph, resource, 'Acme notes exchange', 5)

    assert preview['mode'] == 'search' and preview['records']
    first = preview['records'][0]
    assert 'Acme' in first['content'] and first['rank'] == 1 and first['memory_id']
    assert len(os.listdir('/dev/fd')) <= before              # the store it opened is closed


def test_it_also_works_where_an_event_loop_is_running(studio_data):
    from backend.features.memory import memory_resources, memory_store
    seed(memory_store)
    graph = graph_with_vector_memory()
    resource = memory_resources.resolve(graph, 'mem:notes')

    async def inside_a_loop():
        return memory_resources.read(graph, resource, 'Globex plant', 3)

    hits = asyncio.run(inside_a_loop())
    assert hits and 'Globex' in hits[0]['content']
