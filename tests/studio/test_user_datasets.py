import pytest
from fastapi.testclient import TestClient
from backend.features.data import user_datasets as datasets
from backend.api import sources
from conftest import make_graph, make_task


@pytest.fixture
def client(tmp_path, monkeypatch):
    from backend.api.app import app
    monkeypatch.setattr(datasets, 'data_path', lambda *parts: tmp_path.joinpath(*parts))
    return TestClient(app)


def upload(client, filename='customers.csv', content=b'name,amount\nAlice,10\nBob,20\nCathy,30\n'):
    result = client.post('/api/datasets', files={'file': (filename, content)})
    assert result.status_code == 200, result.text
    return result.json()


def test_upload_persist_rename_delete(client):
    item = upload(client)
    assert item['row_count'] == 3
    assert item['fields'] == ['name', 'amount']
    assert datasets.load(item['id'])['records'][1] == {'name': 'Bob', 'amount': '20'}
    assert client.get('/api/datasets').json()['datasets'][0]['id'] == item['id']
    path = '/api/datasets/' + item['id']
    assert client.patch(path, json={'name': 'My customers'}).json()['name'] == 'My customers'
    assert client.get(path).json()['name'] == 'My customers'
    assert client.delete(path).status_code == 200
    assert client.get(path).status_code == 404
    with pytest.raises(sources.SourceError, match='no longer exists'):
        datasets.records({'dataset_id': item['id']})


@pytest.mark.parametrize('filename,content', [
    ('x.csv', b'a,a\n1,2'), ('x.csv', b'a,b\n1,2,3'), ('x.csv', b'a,b\n1'),
    ('x.json', b'[1]'), ('x.json', b'[]'), ('x.json', b'[{"a":NaN}]'),
    ('x.jsonl', b'{"a":1}\nbroken'), ('x.csv', b'a\n\xff'),
    ('x.xlsx', b'binary'),
])
def test_invalid_data_never_persists(client, filename, content):
    response = client.post('/api/datasets', files={'file': (filename, content)})
    assert response.status_code == 422
    assert client.get('/api/datasets').json() == {'datasets': []}


@pytest.mark.parametrize('filename,content', [
    ('x.tsv', b'a\tb\n1\t2'), ('x.json', b'{"records":[{"a":1,"b":2}]}'),
    ('x.jsonl', b'\xef\xbb\xbf{"a":1,"b":2}\n'),
])
def test_supported_formats(client, filename, content):
    assert upload(client, filename, content)['fields'] == ['a', 'b']


def test_source_mapping_and_limits(client):
    item = upload(client, 'x.json', b'[{"name":"Alice"},{"name":"Bob","amount":20}]')
    config = {'type': 'user_dataset', 'dataset_id': item['id'],
              'field_mapping': {'name': 'customer', 'amount': 'total'}}
    assert sources.records_from_source_node({'source': config}) == [
        {'customer': 'Alice', 'total': None}, {'customer': 'Bob', 'total': 20}]
    assert len(datasets.records({**config, 'n': 1})) == 1
    for mapping in [{'name': 'x', 'amount': 'x'}, {'missing': 'x'}, {'name': ''}]:
        with pytest.raises(sources.SourceError):
            datasets.records({**config, 'field_mapping': mapping})
    with pytest.raises(sources.SourceError):
        datasets.load('../outside')


def test_canvas_batch_uses_every_saved_record_without_collection(client, monkeypatch):
    from backend.api import graphs, batch
    item = upload(client)
    feed = make_task('input', outputs=['customer', 'amount'], kind='source',
                     source={'type': 'user_dataset', 'dataset_id': item['id'],
                             'field_mapping': {'name': 'customer', 'amount': 'amount'}})
    graph = make_graph([feed, make_task('analyze', inputs=['customer', 'amount'], outputs=['answer'])],
                       edges=[('input', 'analyze')], id='own-data')
    graphs.save_graph('own-data', graph)
    result = client.post('/api/graphs/own-data/run-batch/preview', json={'source': 'canvas'})
    assert result.status_code == 200, result.text
    assert result.json()['total'] == 3
    assert not result.json().get('requires_collection')
    captured = {}
    def start(graph, inputs, source, **kwargs):
        captured.update(inputs=inputs, source=source)
        return 'batch-own'
    monkeypatch.setattr(batch, 'start_batch', start)
    result = client.post('/api/graphs/own-data/run-batch', json={'source': 'canvas'})
    assert result.status_code == 200, result.text
    assert [r['customer'] for r in captured['inputs']] == ['Alice', 'Bob', 'Cathy']
    assert captured['source']['dataset_id'] == item['id']


def two_inputs(client):
    from backend.api import graphs
    news = upload(client, 'news.json', b'[{"news":"A"},{"news":"B"},{"news":"C"}]')
    obligors = upload(client, 'obligors.json', b'[{"company":"Alpha"},{"company":"Beta"}]')
    feed = make_task('news', outputs=['news'], kind='source',
                     source={'type':'user_dataset', 'dataset_id':news['id']})
    reference = make_task('obligors', outputs=['obligor_list'], kind='source',
                          source={'type':'user_dataset', 'dataset_id':obligors['id'],
                                  'input_mode':'reference', 'reference_field':'obligor_list'})
    graph = make_graph([reference, feed, make_task('analyze', inputs=['news', 'obligor_list'], outputs=['answer'])],
                       edges=[('news','analyze'), ('obligors','analyze')], id='two-inputs')
    graphs.save_graph(graph['id'], graph)
    return graph, feed, reference


def test_news_batch_broadcasts_complete_obligor_list(client, monkeypatch):
    from backend.api import batch
    graph, feed, reference = two_inputs(client)
    from backend.features.data import input_composition as composition
    assert composition.primary(graph)['name'] == 'news'
    response = client.post('/api/graphs/two-inputs/run-batch/preview', json={'source':'canvas'})
    assert response.status_code == 200, response.text
    assert response.json()['total'] == 3
    captured = {}
    def start(graph, inputs, source, **kwargs):
        captured['inputs'] = inputs
        return 'combined'
    monkeypatch.setattr(batch, 'start_batch', start)
    response = client.post('/api/graphs/two-inputs/run-batch', json={'source':'canvas'})
    assert response.status_code == 200, response.text
    assert [r['news'] for r in captured['inputs']] == ['A','B','C']
    assert all(r['obligor_list'] == [{'company':'Alpha'}, {'company':'Beta'}] for r in captured['inputs'])


def test_single_selected_news_does_not_index_reference_list(client):
    from backend.api.runner import _run_source_nodes
    from backend.api import run_plan
    graph, feed, reference = two_inputs(client)
    assert run_plan._source_of(graph['tasks'], graph['edges'])['node'] == 'news'
    result = _run_source_nodes([reference, feed], {}, {}, record_index=2)
    assert result['news'] == 'C'
    assert len(result['obligor_list']) == 2


def test_reference_collision_and_two_record_streams_are_explicit_errors(client):
    from backend.features.data import input_composition as composition
    graph, feed, reference = two_inputs(client)
    reference['source']['input_mode'] = 'records'
    with pytest.raises(sources.SourceError, match='one per-record'):
        composition.primary(graph)
    with pytest.raises(sources.SourceError, match='overlap'):
        composition.merge([{'news':'A'}], {'news':[]})


def test_collection_snapshots_and_maps_reference_inputs(client):
    from backend.features.data import input_composition as composition
    from backend.api import source_collection
    graph, feed, reference = two_inputs(client)
    snapshot = composition.snapshot(graph, feed)
    records = composition.merge([{'news':'A'}, {'news':'B'}, {'news':'C'}], snapshot)
    mapped, labels = source_collection.mapped_chunk(graph, feed, records, None, None)
    assert len(mapped) == 3 and len(mapped[-1]['obligor_list']) == 2
    old = source_collection.fingerprint(feed, graph)
    reference['source']['reference_field'] = 'changed'
    assert source_collection.fingerprint(feed, graph) != old


@pytest.mark.parametrize('mode', ['all', 'stream'])
def test_collection_reference_snapshot_survives_delete_and_tail(client, monkeypatch, tmp_path, mode):
    from backend.api import source_collection as sc
    from backend.features.data import input_composition as composition
    graph, feed, reference = two_inputs(client)
    monkeypatch.setattr(sc, 'data_path', lambda *parts: tmp_path.joinpath(*parts))
    frozen = composition.snapshot(graph, feed)
    captured = []
    def worker(*args, **kwargs):
        # Delete after snapshot: this collection must still use the captured list.
        datasets.path_for(reference['source']['dataset_id']).unlink()
        for news in ['A','B','C']:
            kwargs['on_record']({'news':news})
        return []
    monkeypatch.setattr(sc.chat_control, 'worker', worker)
    monkeypatch.setattr(sc.batch, 'start_batch', lambda graph, rows, source, **kw: captured.append(rows) or str(len(captured)))
    monkeypatch.setattr(sc.batch, 'wait_for', lambda *a, **kw: True)
    monkeypatch.setattr(sc.batch, 'get_batch', lambda *a, **kw: {'status':'succeeded'})
    job = {'id':'f'*32, 'graph_id':graph['id'], 'mode':mode, 'batch_size':2,
           'records':[], 'record_count':0, 'reference_inputs':frozen, 'batches':[],
           'submitted_records':0, 'config':feed['source'], 'status':'collecting'}
    sc.execute_collection(job, graph, feed, sc.chat_control.Control(), 1, None, None)
    assert job['status'] == ('completed' if mode == 'stream' else 'ready'), job.get('error')
    assert all(len(row['obligor_list']) == 2 for row in job['records'])
    assert [len(chunk) for chunk in captured] == ([2,1] if mode == 'stream' else [])
    if captured:
        assert captured[-1][0]['news'] == 'C' and len(captured[-1][0]['obligor_list']) == 2
