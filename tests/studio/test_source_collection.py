import json
import time
import pytest
from conftest import make_graph, make_task


def test_news_windows_fetch_all_before_return_and_skip_empty(monkeypatch):
    from backend.api import source_apis as api
    calls = []
    def fetch(config, start, end):
        calls.append((start.isoformat(), end.isoformat()))
        assert config['max_records'] == 250
        return {'n_articles': 0 if start.day == 2 else 1, 'news_batch': f'[{start}] news'}
    monkeypatch.setattr(api, 'fetch_gdelt_news', fetch)
    progress = []
    records = api.collect_gdelt_records({'query': 'Test', 'start_date': '2026-01-01', 'end_date': '2026-01-03'}, lambda a,b: progress.append((a,b)))
    assert len(calls) == 3
    assert [r['as_of'] for r in records] == ['2026-01-01', '2026-01-03']
    assert progress[-1] == (3, 3)


def test_news_splits_capped_windows_and_refuses_capped_day(monkeypatch):
    from backend.api import source_apis as api
    def fetch(config, start, end):
        return {'n_articles': 250 if start != end else 1, 'news_batch': f'[{start}] news'}
    monkeypatch.setattr(api, 'fetch_gdelt_news', fetch)
    config = {'query': 'Test', 'start_date': '2026-01-01', 'end_date': '2026-01-03', 'batch_step': 'weekly'}
    assert api.collect_gdelt_records(config)[0]['n_articles'] == 3
    monkeypatch.setattr(api, 'fetch_gdelt_news', lambda *a, **k: {'n_articles': 250})
    with pytest.raises(api.SourceError, match='incomplete'):
        api.collect_gdelt_records(config)


def test_http_array_not_truncated(monkeypatch):
    from backend.api import source_apis as api
    items = [{'text': 'x' * 9000}, {'text': 'y'}]
    monkeypatch.setattr(api, 'http_request', lambda *a, **k: (200, json.dumps({'items': items}).encode()))
    records = api.fetch_http_api({'url': 'https://example.test', 'extract': 'items'}, split_records=True)
    assert [json.loads(r['api_response']) for r in records] == items


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from backend.api import app, graphs, source_collection as sc
    monkeypatch.setattr(graphs, 'GRAPHS_DIR', tmp_path / 'graphs')
    monkeypatch.setattr(sc, 'data_path', lambda name: tmp_path / name)
    sc._jobs.clear()
    feed = make_task('news', outputs=['company', 'news_batch'])
    feed.update(kind='source', source={'type': 'gdelt_news', 'query': 'Test'})
    graph = make_graph([feed, make_task('judge', inputs=['company', 'news_batch'], outputs=['verdict'])], edges=[('news', 'judge')], id='collect-test')
    graphs.save_graph('collect-test', graph)
    return TestClient(app.app)


def settle(client, id):
    for _ in range(100):
        job = client.get(f'/api/graphs/collect-test/source-collections/{id}').json()
        if job['status'] != 'collecting': return job
        time.sleep(.01)
    pytest.fail('Collection did not finish')


def test_collect_preview_run_reuses_snapshot_and_detects_config_change(client, monkeypatch):
    from backend.api import app, source_collection as sc, graphs
    calls = []
    records = [{'company': 'Test', 'news_batch': f'day{i}', 'as_of': f'2026-01-0{i}'} for i in (1,2)]
    def worker(*a, **kw):
        calls.append(a)
        return records
    monkeypatch.setattr(sc.chat_control, 'worker', worker)
    root = '/api/graphs/collect-test/'
    assert client.post(root+'run-batch/preview', json={'source':'canvas'}).json()['requires_collection']
    assert calls == []
    assert client.post(root+'run-batch', json={'source':'canvas'}).status_code == 422
    id = client.post(root+'source-collections', json={}).json()['id']
    assert settle(client,id)['record_count'] == 2
    sc._jobs.clear()  # Reload persisted snapshot after restart.
    body = {'source':'canvas', 'collection_id':id}
    assert client.post(root+'run-batch/preview', json=body).json()['total'] == 2
    captured = []
    monkeypatch.setattr(app.batch_store, 'start_batch', lambda g,r,s,**kw: captured.append(r) or 'batch-test')
    assert client.post(root+'run-batch', json=body).status_code == 200
    assert len(calls) == 1
    # GDELT declares no trajectory, so only the node's wired outputs reach
    # the batch; nothing domain-specific (sample_id) is carried along.
    assert captured[0] == [{k: r[k] for k in ('company', 'news_batch')} for r in records]
    graph = graphs.load_graph('collect-test')
    graph['tasks'][0]['source']['query'] = 'Changed'
    graphs.save_graph('collect-test',graph)
    assert client.post(root+'run-batch',json=body).status_code == 422


def test_cancel_and_failure_never_make_records_runnable(client, monkeypatch):
    from backend.api import source_collection as sc
    def worker(*a, **kw):
        while True:
            sc.chat_control.check()
            time.sleep(.01)
    monkeypatch.setattr(sc.chat_control,'worker',worker)
    root='/api/graphs/collect-test/'
    id=client.post(root+'source-collections',json={}).json()['id']
    assert client.post(root+f'source-collections/{id}/stop').status_code == 200
    assert settle(client,id)['status'] == 'cancelled'
    assert client.post(root+'run-batch',json={'source':'canvas','collection_id':id}).status_code == 422


def test_real_collection_worker_reads_http_items():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    from backend.api import chat_control
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"items":[{"value":1},{"value":2}]}')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        records = chat_control.worker('collect', {'source': {'type': 'http_api', 'url': f'http://127.0.0.1:{server.server_port}', 'extract': 'items', 'batch_items': 'items'}})
        assert [json.loads(r['api_response']) for r in records] == [{'value':1},{'value':2}]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_news_error_response_is_not_treated_as_an_empty_window(monkeypatch):
    from backend.api import source_apis as api
    monkeypatch.setattr(api, '_get_json', lambda *a, **kw: (200, {'error': 'Unavailable'}))
    with pytest.raises(api.SourceError, match='cannot be considered complete'):
        api.fetch_gdelt_news({'query': 'Acme'})


def test_stream_starts_before_collection_finishes_and_flushes_tail(client, monkeypatch):
    import threading
    from backend.api import source_collection as sc
    first_started = threading.Event()
    collection_done = threading.Event()
    chunks = []
    def start(graph, records, source, **kwargs):
        chunks.append(records)
        first_started.set()
        return str(len(chunks))
    def worker(*args, on_record, **kwargs):
        records = [{'company':'Test', 'news_batch':str(i)} for i in range(7)]
        for r in records[:3]: on_record(r)
        assert first_started.wait(2), 'First batch waited for the whole source'
        for r in records[3:]: on_record(r)
        collection_done.set()
        return records
    monkeypatch.setattr(sc.chat_control, 'worker', worker)
    monkeypatch.setattr(sc.batch, 'start_batch', start)
    monkeypatch.setattr(sc.batch, 'wait_for', lambda *a, **kw: collection_done.wait(.01))
    monkeypatch.setattr(sc.batch, 'get_batch', lambda id: {'status':'succeeded'})
    job=client.post('/api/graphs/collect-test/source-collections',json={'mode':'stream','batch_size':3}).json()
    result=settle(client,job['id'])
    assert result['status']=='completed', result
    assert [len(c) for c in chunks]==[3,3,1]
    assert [r['news_batch'] for c in chunks for r in c]==[str(i) for i in range(7)]
    assert result['submitted_records']==7
    assert client.post('/api/graphs/collect-test/run-batch',json={'source':'canvas','collection_id':job['id']}).status_code==422


def test_all_mode_does_not_start_batches_as_records_arrive(client, monkeypatch):
    from backend.api import source_collection as sc
    def worker(*args,on_record,**kwargs):
        records=[{'company':'Test','news_batch':str(i)} for i in range(3)]
        for r in records: on_record(r)
        return records
    monkeypatch.setattr(sc.chat_control,'worker',worker)
    monkeypatch.setattr(sc.batch,'start_batch',lambda *a,**kw: pytest.fail('Collect-all started a batch'))
    id=client.post('/api/graphs/collect-test/source-collections',json={'mode':'all','batch_size':1}).json()['id']
    assert settle(client,id)['status']=='ready'


def test_stop_stream_cancels_active_batch_and_does_not_launch_next(client, monkeypatch):
    import threading
    from backend.api import source_collection as sc
    started=threading.Event()
    cancelled=[]
    def start(*a,**kw):
        assert not started.is_set(), 'Started a second batch after stop'
        started.set()
        return 'active-batch'
    def worker(*args,on_record,**kwargs):
        for i in range(3): on_record({'company':'Test','news_batch':str(i)})
        while True:
            sc.chat_control.check()
            time.sleep(.01)
    monkeypatch.setattr(sc.chat_control,'worker',worker)
    monkeypatch.setattr(sc.batch,'start_batch',start)
    monkeypatch.setattr(sc.batch,'wait_for',lambda *a,**kw: (time.sleep(.01) or False))
    monkeypatch.setattr(sc.batch,'cancel_batch',lambda id: cancelled.append(id))
    monkeypatch.setattr(sc.batch,'get_batch',lambda id: {'status':'cancelled'})
    root='/api/graphs/collect-test/'
    id=client.post(root+'source-collections',json={'mode':'stream','batch_size':1}).json()['id']
    assert started.wait(2)
    client.post(root+f'source-collections/{id}/stop')
    assert settle(client,id)['status']=='cancelled'
    assert cancelled==['active-batch']


def test_failed_stream_batch_blocks_later_batches(client, monkeypatch):
    from backend.api import source_collection as sc
    chunks=[]
    def worker(*args,on_record,**kwargs):
        records=[{'company':'Test','news_batch':str(i)} for i in range(5)]
        for r in records: on_record(r)
        return records
    monkeypatch.setattr(sc.chat_control,'worker',worker)
    monkeypatch.setattr(sc.batch,'start_batch',lambda g,r,s,**kw: chunks.append(r) or 'failed-batch')
    monkeypatch.setattr(sc.batch,'wait_for',lambda *a,**kw: True)
    monkeypatch.setattr(sc.batch,'get_batch',lambda id: {'status':'failed'})
    monkeypatch.setattr(sc.batch,'cancel_batch',lambda id: {})
    id=client.post('/api/graphs/collect-test/source-collections',json={'mode':'stream','batch_size':2}).json()['id']
    assert settle(client,id)['status']=='failed'
    assert len(chunks)==1


def test_a_stopped_collection_still_reports_its_batch_evaluators(client, monkeypatch):
    """Stopping part way does not throw away the report for what did run."""
    import threading
    from backend.api import source_collection as sc
    started = threading.Event()
    seen = {}

    def worker(*args, on_record, **kwargs):
        for i in range(3):
            on_record({'company': 'Test', 'news_batch': str(i)})
        while True:
            sc.chat_control.check()
            time.sleep(.01)

    def fake_evaluate_runs(graph, runs, timing=None):
        seen['runs'] = len(runs)
        return {'report': {'status': 'success', 'metrics': {'score': 1.0}}}

    monkeypatch.setattr(sc.chat_control, 'worker', worker)
    monkeypatch.setattr(sc.batch, 'start_batch', lambda *a, **kw: started.set() or 'active-batch')
    monkeypatch.setattr(sc.batch, 'wait_for', lambda *a, **kw: (time.sleep(.01) or False))
    monkeypatch.setattr(sc.batch, 'cancel_batch', lambda id: {})
    monkeypatch.setattr(sc.batch, 'get_batch',
                        lambda id: {'status': 'cancelled', 'items': [{'run_id': None, 'status': 'cancelled'}]})
    monkeypatch.setattr('backend.features.evaluation.evaluator_tools.evaluate_runs', fake_evaluate_runs)
    root = '/api/graphs/collect-test/'
    id = client.post(root + 'source-collections', json={'mode': 'stream', 'batch_size': 1}).json()['id']
    assert started.wait(2)
    client.post(root + f'source-collections/{id}/stop')
    job = settle(client, id)

    assert job['status'] == 'cancelled'
    assert seen['runs'] == 1                     # the one batch that ran
    assert job['evaluations']['report']['status'] == 'success'
