"""A batch must not run the process out of file handles ("Too many open
files"): every memory store a run opens is closed when the run ends, not
whenever the garbage collector gets to it."""
import asyncio
import gc
import os
import random
import sys

import pytest

from conftest import make_graph, make_task

pytestmark = pytest.mark.skipif(not os.path.isdir('/dev/fd'), reason='counts /dev/fd')


def open_files():
    return len(os.listdir('/dev/fd'))


def test_a_node_by_node_batch_with_memory_leaves_no_files_open(studio_data, monkeypatch):
    from evoagentx.models import LiteLLMConfig
    from backend.api import batch, runner, tools_registry
    from backend.features.memory import memory_store

    async def answer(agent, task, inputs, state):
        await asyncio.sleep(random.uniform(0, 0.02))
        return {o['name']: f"{task['name']} noted" for o in task.get('outputs') or []}

    class Stub:
        config = LiteLLMConfig(model='deepseek/deepseek-chat', deepseek_key='test-only')
    monkeypatch.setattr(runner, 'execute_llm_node', answer)
    monkeypatch.setattr(runner, '_make_llm', lambda **kw: Stub())
    monkeypatch.setattr(tools_registry, 'validate_tool_names', lambda names: None)
    monkeypatch.setattr(tools_registry, 'resolve_tools', lambda names, **kw: None)
    monkeypatch.setattr(batch, '_pause', lambda seconds, batch_id=None: None)
    a = make_task('a', inputs=['id'], outputs=['x'], use_long_term_memory=True)
    b = make_task('b', inputs=['x'], outputs=['y'], use_long_term_memory=True, memory={'read_from': ['a', 'b']})
    graph = make_graph([a, b], [('a', 'b')])
    graph['id'] = 'open-files'

    def run(count):
        batch_id = batch.start_batch(graph, [{'id': f'r{i}'} for i in range(count)],
                                     {'type': 'canvas', 'config': {'type': 'dataloader', 'read_batch_size': count}},
                                     workers=4, mode='node')
        assert batch.wait_for(batch_id, timeout=120)
        assert batch.get_batch(batch_id)['status'] == 'succeeded'

    run(2)                       # the embedding model and imports, loaded once
    before = open_files()
    gc.disable()                 # what the collector would free is not the fix
    try:
        run(12)
        left = open_files() - before
    finally:
        gc.enable()
    assert left <= 3, f'{left} files left open by a 12-record batch'
    assert len(memory_store.list_entries('open-files', 'a')) == 14


def test_close_memory_closes_the_store_and_tolerates_anything(studio_data):
    from backend.features.memory import memory_store
    store = memory_store.open_memory('closing', 'node', create=True)
    connection = store.storage_handler.storageDB.connection
    memory_store.close_memory(store)
    with pytest.raises(Exception):
        connection.execute('select 1')
    memory_store.close_memory(store)          # twice is fine
    memory_store.close_memory(None)
    memory_store.close_memory(object())


def test_the_server_raises_its_open_file_limit():
    resource = pytest.importorskip('resource')
    from backend.api import studio_config
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
        assert studio_config.raise_open_file_limit() > 256
        assert resource.getrlimit(resource.RLIMIT_NOFILE)[0] > 256
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_a_stop_during_the_memory_writes_writes_nothing_more(studio_data, monkeypatch):
    """Every record of a chunk writes its memory after the last node, one
    after another under the store's lock — embedding, indexing, saving. A
    Stop then must end the batch now, not after the whole queue has written."""
    import threading
    import time
    from evoagentx.models import LiteLLMConfig
    from backend.api import batch, runner, tools_registry

    async def answer(agent, task, inputs, state):
        return {o['name']: f"{task['name']} noted" for o in task.get('outputs') or []}

    class Stub:
        config = LiteLLMConfig(model='deepseek/deepseek-chat', deepseek_key='test-only')
    monkeypatch.setattr(runner, 'execute_llm_node', answer)
    monkeypatch.setattr(runner, '_make_llm', lambda **kw: Stub())
    monkeypatch.setattr(tools_registry, 'validate_tool_names', lambda names: None)
    monkeypatch.setattr(tools_registry, 'resolve_tools', lambda names, **kw: None)
    writes, first = [], threading.Event()
    real_write = runner._write_entry

    def slow_write(*a, **kw):
        writes.append(time.monotonic())
        first.set()
        time.sleep(0.3)
        return real_write(*a, **kw)
    monkeypatch.setattr(runner, '_write_entry', slow_write)
    graph = make_graph([make_task('a', inputs=['id'], outputs=['x'], use_long_term_memory=True)])
    graph['id'] = 'stop-writes'
    batch_id = batch.start_batch(graph, [{'id': f'r{i}'} for i in range(8)],
                                 {'type': 'canvas', 'config': {'type': 'dataloader', 'read_batch_size': 8}},
                                 workers=4, mode='node')
    assert first.wait(60)
    stopped_at = time.monotonic()
    batch.cancel_batch(batch_id)
    assert batch.wait_for(batch_id, timeout=10)
    assert time.monotonic() - stopped_at < 3
    assert sum(1 for at in writes if at > stopped_at) == 0
    items = batch.get_batch(batch_id)['items']
    assert sum(i['status'] == 'cancelled' for i in items) >= 6       # all but the one mid-write


def test_a_chunk_shares_one_open_store_and_saves_it_once_without_losing_an_entry(studio_data, monkeypatch):
    """Opening a store loads its corpus and saving rewrites it: per record,
    that made the end of every chunk a queue of full loads and rewrites.
    Now the records of a chunk read one shared copy and the last of them
    saves once for all — and every entry is there."""
    from evoagentx.core.message import Message
    from evoagentx.memory.long_term_memory import LongTermMemory
    from evoagentx.models import LiteLLMConfig
    from backend.api import batch, runner, tools_registry
    from backend.features.memory import memory_store

    async def answer(agent, task, inputs, state):
        return {o['name']: f"{task['name']} noted {inputs}" for o in task.get('outputs') or []}

    class Stub:
        config = LiteLLMConfig(model='deepseek/deepseek-chat', deepseek_key='test-only')
    monkeypatch.setattr(runner, 'execute_llm_node', answer)
    monkeypatch.setattr(runner, '_make_llm', lambda **kw: Stub())
    monkeypatch.setattr(tools_registry, 'validate_tool_names', lambda names: None)
    monkeypatch.setattr(tools_registry, 'resolve_tools', lambda names, **kw: None)
    store = memory_store.open_memory('shared', 'a', create=True)
    store.add([Message(content=f'earlier {i}', agent='a') for i in range(20)])
    store.save()
    memory_store.close_memory(store)
    opens, saves = [], []
    real_open, real_save = memory_store.open_memory, LongTermMemory.save
    monkeypatch.setattr(memory_store, 'open_memory', lambda *a, **kw: opens.append(1) or real_open(*a, **kw))
    monkeypatch.setattr(LongTermMemory, 'save', lambda self, *a, **kw: saves.append(1) or real_save(self, *a, **kw))
    graph = make_graph([make_task('a', inputs=['id'], outputs=['x'], use_long_term_memory=True)])
    graph['id'] = 'shared'

    batch_id = batch.start_batch(graph, [{'id': f'r{i}'} for i in range(12)],
                                 {'type': 'canvas', 'config': {'type': 'dataloader', 'read_batch_size': 12}},
                                 workers=4, mode='node')
    assert batch.wait_for(batch_id, timeout=120)
    assert batch.get_batch(batch_id)['status'] == 'succeeded'
    assert len(opens) <= 3, f'{len(opens)} opens for one chunk'      # one to read, one to write (+1 slack)
    assert len(saves) <= 2, f'{len(saves)} saves for one chunk'
    assert len(memory_store.list_entries('shared', 'a')) == 32        # nothing lost
