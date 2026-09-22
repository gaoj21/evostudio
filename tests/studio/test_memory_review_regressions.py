"""Regression coverage for shared ordering, rename identity and explicit keys."""
from pathlib import Path
import time
from backend.features.memory import memory_policy as policy, table_store as store, identity

V2 = {'version': 2, 'kind': 'table', 'write_mode': 'append', 'time_filter': False}


def test_explicit_update_key_does_not_require_an_optional_entity():
    task = {'name': 'node', 'memory': {**V2, 'write_mode': 'upsert', 'key': 'ticket_id', 'match': 'customer'}}
    payload = policy.select(task, {}, {'reply': 'ok'}, run_data={'ticket_id': 'T-1'})
    row = policy.table_write(task, payload)
    assert row['key'] == 'T-1' and row['subject'] == ''


def test_node_rename_preserves_table_and_vector_store_identity(tmp_path, monkeypatch):
    from backend.api import graphs
    from backend.features.memory import memory_store
    graph = {'id': 'rename-test', 'tasks': [{'name': 'old', 'use_long_term_memory': True, 'memory': dict(V2)}]}
    monkeypatch.setattr(graphs, 'load_graph', lambda _: graph)
    monkeypatch.setattr(store, 'TABLES_DIR', tmp_path)
    store.forget()
    store.write(graph['id'], 'old', '', '', {'outputs': {'reply': 'kept'}}, 'now', execution_id='one')
    old_path = store.path(graph['id'], 'old')
    old_corpus = memory_store.corpus_id(graph['id'], 'old')
    graph['tasks'][0]['name'] = 'new'
    identity.rename_references(graph, 'old', 'new')
    assert store.path(graph['id'], 'new') == old_path
    assert store.count(graph['id'], 'new') == 1
    assert store.nodes(graph['id']) == ['new']
    assert memory_store.corpus_id(graph['id'], 'new') == old_corpus
    graph['tasks'][0]['name'] = 'third'
    identity.rename_references(graph, 'new', 'third')
    assert store.count(graph['id'], 'third') == 1


def test_chat_rename_rewrites_metadata_references():
    graph = {'tasks': [{'name': 'renamed'}, {'name': 'reader', 'memory': {
        **V2, 'at': 'nodes.source.outputs.when', 'match': 'nodes.source.outputs.id',
        'key': 'nodes.source.outputs.key', 'context': ['nodes.source.outputs.context']}}]}
    identity.rename_references(graph, 'source', 'renamed')
    p = graph['tasks'][1]['memory']
    assert p['at'] == 'nodes.renamed.outputs.when'
    assert p['match'] == 'nodes.renamed.outputs.id'
    assert p['key'] == 'nodes.renamed.outputs.key'
    assert p['context'] == ['nodes.renamed.outputs.context']


def test_loader_groups_cannot_override_global_memory_order(tmp_path, monkeypatch):
    from backend.features.execution import batch
    monkeypatch.setattr(store, 'TABLES_DIR', tmp_path / 'memory')
    monkeypatch.setattr(batch, 'BATCHES_DIR', tmp_path / 'batches')
    store.forget()
    task = {'name': 'n', 'use_long_term_memory': True, 'memory': V2}
    graph = {'id': 'ordering-test', 'tasks': [task], 'edges': []}
    seen, runs = {}, {}
    def run(graph, record, run_id, **kwargs):
        seen[record['number']] = store.count(graph['id'], 'n')
        time.sleep(.03)
        store.write(graph['id'], 'n', '', '', {'outputs': record}, 'now', execution_id=run_id)
        runs[run_id] = {'status': 'success', 'result': {}}
    monkeypatch.setattr(batch.runner, 'start_run', run)
    monkeypatch.setattr(batch.runner, 'get_run', lambda rid: runs.get(rid))
    bid = batch.start_batch(graph, [{'number': 1, '_dataloader': {'group': 'A'}},
                                  {'number': 2, '_dataloader': {'group': 'B'}}], {'type': 'upload'}, workers=2)
    assert batch.wait_for(bid, timeout=10)
    assert seen == {1: 0, 2: 1}
    assert batch.get_batch(bid)['status'] == 'succeeded'


def test_chat_resource_id_survives_node_rename(tmp_path, monkeypatch):
    from backend.api import graphs
    from backend.features.memory import memory_resources
    graph = {'id': 'resource-rename', 'tasks': [{'name': 'renamed', 'use_long_term_memory': True,
             'memory': {**V2, 'store_id': 'original'}}]}
    monkeypatch.setattr(graphs, 'load_graph', lambda _: graph)
    monkeypatch.setattr(store, 'TABLES_DIR', tmp_path)
    monkeypatch.setattr(memory_resources.mem0_service, 'spaces', lambda _: [])
    store.forget()
    store.write(graph['id'], 'renamed', '', '', {'outputs': {'reply': 'retained'}}, 'now', execution_id='one')
    resource = memory_resources.resolve(graph, 'mem:original')
    assert resource['name'] == 'renamed memory'
    assert memory_resources.read(graph, resource)[0]['content']['outputs']['reply'] == 'retained'


def test_running_snapshot_keeps_its_memory_identity_if_saved_graph_changes(monkeypatch):
    from backend.api import graphs
    old = {'id': 'g', 'tasks': [{'name': 'n', 'memory': {'store_id': 'before'}}]}
    new = {'id': 'g', 'tasks': [{'name': 'n', 'memory': {'store_id': 'after'}}]}
    monkeypatch.setattr(graphs, 'load_graph', lambda _: new)
    @identity.execution_snapshot
    def execute(run_id, graph):
        return identity.storage_name('g', 'n')
    assert execute('run', old) == 'before'
    assert identity.storage_name('g', 'n') == 'after'
