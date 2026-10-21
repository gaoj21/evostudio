"""Listing runs and batches reads small views of each saved file, kept on disk
so a restart does not parse every run again, and pages them."""
import json
import os

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import batch, result_chat, runner
from backend.features import persistence


def _write(path, value):
    persistence.write_json(path, value)


def _counting(calls):
    def make(document):
        calls.append(document.get('id'))
        return {'id': document.get('id'), 'size': len(document.get('items') or [])}
    return make


def _restart():
    """What a restart leaves: the disk cache, nothing in memory."""
    persistence.clear_memory_views()
    persistence._disk_views().close()


def test_view_outlives_a_restart(tmp_path):
    path = tmp_path / 'doc.json'
    _write(path, {'id': 'a', 'items': [1, 2, 3]})
    calls = []
    assert persistence.file_view(path, 'shape', _counting(calls), persist=True) == {'id': 'a', 'size': 3}
    _restart()
    assert persistence.file_view(path, 'shape', _counting(calls), persist=True) == {'id': 'a', 'size': 3}
    assert calls == ['a']
    assert os.path.isfile(persistence.VIEW_CACHE)


def test_view_is_recomputed_when_the_file_changes(tmp_path):
    path = tmp_path / 'doc.json'
    _write(path, {'id': 'a', 'items': [1]})
    calls = []
    persistence.file_view(path, 'shape', _counting(calls), persist=True)
    _write(path, {'id': 'a', 'items': [1, 2, 3, 4]})
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    _restart()
    assert persistence.file_view(path, 'shape', _counting(calls), persist=True)['size'] == 4
    assert calls == ['a', 'a']


def test_a_view_not_asked_to_persist_stays_in_memory(tmp_path):
    path = tmp_path / 'doc.json'
    _write(path, {'id': 'a'})
    calls = []
    persistence.file_view(path, 'shape', _counting(calls))
    _restart()
    persistence.file_view(path, 'shape', _counting(calls))
    assert calls == ['a', 'a']


def test_corrupt_cache_is_replaced(tmp_path):
    cache = persistence.Path(persistence.VIEW_CACHE)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(b'not a database at all' * 100)
    _restart()
    path = tmp_path / 'doc.json'
    _write(path, {'id': 'a'})
    calls = []
    assert persistence.file_view(path, 'shape', _counting(calls), persist=True)['id'] == 'a'
    _restart()
    assert persistence.file_view(path, 'shape', _counting(calls), persist=True)['id'] == 'a'
    assert calls == ['a']


def test_unusable_cache_falls_back_to_reading_the_file(tmp_path, monkeypatch):
    blocked = tmp_path / 'blocked'
    blocked.write_text('a file where the cache directory should be')
    monkeypatch.setattr(persistence, 'VIEW_CACHE', blocked / 'cache' / 'views.sqlite')
    path = tmp_path / 'doc.json'
    _write(path, {'id': 'a'})
    calls = []
    for _ in range(2):
        _restart()
        assert persistence.file_view(path, 'shape', _counting(calls), persist=True)['id'] == 'a'
    assert calls == ['a', 'a']


def test_cache_holding_a_bad_value_is_ignored(tmp_path):
    path = tmp_path / 'doc.json'
    _write(path, {'id': 'a'})
    calls = []
    persistence.file_view(path, 'shape', _counting(calls), persist=True)
    connection = persistence._disk_views()._connect()
    connection.execute("UPDATE views SET value = '{broken'")
    _restart()
    assert persistence.file_view(path, 'shape', _counting(calls), persist=True)['id'] == 'a'
    assert calls == ['a', 'a']


def _run(run_id, created_at, graph_id='g', batch_id=None, status='success', customer='C'):
    return {'run_id': run_id, 'graph_id': graph_id, 'status': status,
            'nodes': [{'name': 'reader', 'status': 'completed', 'output': {'customer': customer, 'body': 'x' * 500}}],
            'inputs': {}, 'created_at': created_at, 'batch_id': batch_id}


def test_saving_a_run_records_what_listings_need(monkeypatch):
    runner.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    runner._persist_run(_run('r1', '2026-01-01T00:00:00', batch_id='b'))
    _restart()
    monkeypatch.setattr(persistence.json, 'load', lambda stream: (_ for _ in ()).throw(AssertionError('parsed')))
    chosen, _live = runner._run_index('g')
    assert [(run_id, meta) for run_id, meta, _ in chosen] == [
        ('r1', {'graph_id': 'g', 'created_at': '2026-01-01T00:00:00', 'batch_id': 'b', 'status': 'success'})]


def _client():
    app = FastAPI()
    app.include_router(result_chat.router)
    return TestClient(app)


def _seed():
    runner.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    for i in range(12):
        _write(runner.RUNS_DIR / f'r{i:02d}.json',
               _run(f'r{i:02d}', f'2026-01-{i + 1:02d}T00:00:00', batch_id='b1' if i % 3 == 0 else None,
                    status='failed' if i == 4 else 'success', customer='Zeta' if i == 7 else f'C{i}'))
    _write(runner.RUNS_DIR / 'other.json', _run('other', '2026-02-01T00:00:00', graph_id='h'))


def test_results_are_paged_newest_first():
    _seed()
    client = _client()
    first = client.get('/api/graphs/g/results', params={'summary': 1, 'page': 1, 'limit': 5}).json()
    assert [r['run_id'] for r in first['runs']] == ['r11', 'r10', 'r09', 'r08', 'r07']
    assert first['total'] == 12 and first['has_more'] is True
    assert 'body' not in first['runs'][0]['nodes'][0]['output']
    last = client.get('/api/graphs/g/results', params={'summary': 1, 'page': 1, 'limit': 5, 'offset': 10}).json()
    assert [r['run_id'] for r in last['runs']] == ['r01', 'r00']
    assert last['has_more'] is False


def test_results_filter_by_batch_and_search():
    _seed()
    client = _client()
    get = lambda **params: client.get('/api/graphs/g/results', params={'summary': 1, 'page': 1, **params}).json()
    in_batch = get(batch='b1')
    assert [r['run_id'] for r in in_batch['runs']] == ['r09', 'r06', 'r03', 'r00'] and in_batch['total'] == 4
    assert get(batch='single')['total'] == 8
    found = get(q='zeta')
    assert [r['run_id'] for r in found['runs']] == ['r07'] and found['total'] == 1
    assert [r['run_id'] for r in get(q='FAILED')['runs']] == ['r04']
    assert [r['run_id'] for r in get(q='r1')['runs']] == ['r11', 'r10']
    both = get(q='r0', batch='b1', limit=2)
    assert [r['run_id'] for r in both['runs']] == ['r09', 'r06'] and both['total'] == 4 and both['has_more']
    assert get(q='nothing like it') == {'runs': [], 'total': 0, 'has_more': False}


def test_results_without_page_stay_a_list():
    _seed()
    listed = _client().get('/api/graphs/g/results', params={'summary': 1}).json()
    assert isinstance(listed, list) and len(listed) == 12
    assert listed[0]['run_id'] == 'r11'
    # Called directly, as other modules do: Query markers stand for defaults.
    assert len(result_chat.list_results('g', summary_only=True)) == 12
    page = result_chat.list_results('g', summary_only=True, page=True)
    assert page['total'] == 12 and len(page['runs']) == 12


def test_run_summaries_outlive_a_restart(monkeypatch):
    _seed()
    client = _client()
    params = {'summary': 1, 'page': 1, 'q': 'zeta'}
    expected = client.get('/api/graphs/g/results', params=params).json()
    _restart()
    monkeypatch.setattr(persistence.json, 'load', lambda stream: (_ for _ in ()).throw(AssertionError('parsed')))
    assert client.get('/api/graphs/g/results', params=params).json() == expected


def test_batch_listing_outlives_a_restart(monkeypatch):
    batch.BATCHES_DIR.mkdir(parents=True, exist_ok=True)
    _write(batch.BATCHES_DIR / 'b1.json', {'batch_id': 'b1', 'graph_id': 'g', 'status': 'success',
                                            'created_at': '2026-01-01T00:00:00',
                                            'items': [{'status': 'success', 'outputs': {'x': 'y' * 1000}}] * 5})
    expected = batch.list_batches('g')
    assert expected[0]['counts'] == {'success': 5}
    _restart()
    monkeypatch.setattr(persistence.json, 'load', lambda stream: (_ for _ in ()).throw(AssertionError('parsed')))
    assert json.loads(json.dumps(batch.list_batches('g'))) == json.loads(json.dumps(expected))


def _opened(monkeypatch):
    """The JSON files opened from here on."""
    import builtins
    opened = []
    real = builtins.open

    def spy(file, *args, **kwargs):
        if str(file).endswith('.json'):
            opened.append(os.path.basename(str(file)))
        return real(file, *args, **kwargs)
    monkeypatch.setattr(builtins, 'open', spy)
    return opened


def test_restart_opens_only_runs_that_may_be_in_flight(monkeypatch):
    runner.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    runner._persist_run(_run('done', '2026-01-01T00:00:00'))
    runner._persist_run(_run('live', '2026-01-02T00:00:00', status='running'))
    _write(runner.RUNS_DIR / 'unknown.json', _run('unknown', '2026-01-03T00:00:00'))
    _restart()
    opened = _opened(monkeypatch)
    assert runner.mark_interrupted() == 1
    assert sorted(opened) == ['live.json', 'unknown.json']
    assert json.loads((runner.RUNS_DIR / 'live.json').read_text())['status'] == 'failed'
    # What was read is remembered: the next restart opens nothing.
    _restart()
    opened.clear()
    assert runner.mark_interrupted() == 0
    assert opened == []


def test_a_run_changed_since_its_view_was_kept_is_opened(monkeypatch):
    runner.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    runner._persist_run(_run('r', '2026-01-01T00:00:00'))
    path = runner.RUNS_DIR / 'r.json'
    # Another process left it running, after this view was kept.
    _write(path, _run('r', '2026-01-01T00:00:00', status='running', customer='longer name'))
    _restart()
    opened = _opened(monkeypatch)
    assert runner.mark_interrupted() == 1
    assert opened == ['r.json']


def test_restart_opens_only_batches_that_may_be_in_flight(monkeypatch):
    batch.BATCHES_DIR.mkdir(parents=True, exist_ok=True)
    for batch_id, status in (('done', 'success'), ('live', 'running')):
        _write(batch.BATCHES_DIR / f'{batch_id}.json', {
            'batch_id': batch_id, 'graph_id': 'g', 'status': status, 'created_at': '2026-01-01T00:00:00',
            'items': [{'index': 0, 'status': 'running'}]})
    batch.list_batches('g')            # the history list keeps each digest
    _restart()
    opened = _opened(monkeypatch)
    assert batch.mark_interrupted() == 1
    assert opened == ['live.json']
    _restart()
    opened.clear()
    assert batch.mark_interrupted() == 0
    assert opened == []


def test_concurrent_listings_and_cleanup_read_each_run_file_once(monkeypatch):
    """With the cache empty, the startup cleanup, a task's run list and its
    run plan each scanned every run file at once — three passes over a synced
    folder. One scans; the others find the views it cached."""
    import threading
    runner.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    for i in range(30):
        _write(runner.RUNS_DIR / f'r{i:02d}.json', _run(f'r{i:02d}', f'2026-01-{i % 28 + 1:02d}T00:00:00'))
    _restart()
    opened = _opened(monkeypatch)
    start = threading.Barrier(4)

    def call(work):
        start.wait()
        work()
    workers = [threading.Thread(target=call, args=(work,)) for work in (
        runner.mark_interrupted, lambda: runner._run_index('g'),
        lambda: runner._run_index('g'), lambda: runner._run_index(None))]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)
    assert sorted(opened) == sorted(f'r{i:02d}.json' for i in range(30))


def test_run_plan_prefill_stops_at_the_newest_run_that_covers_every_input(monkeypatch):
    from backend.api import app as app_module
    runs = [{'run_id': f'r{i}', 'inputs': {'topic': f't{i}'}, 'nodes': []} for i in range(5)]
    read = []
    monkeypatch.setattr(runner, 'list_runs', lambda graph_id=None, limit=20: [{'run_id': r['run_id']} for r in runs])
    monkeypatch.setattr(runner, 'get_run', lambda run_id: read.append(run_id) or next(r for r in runs if r['run_id'] == run_id))
    prefill = app_module._prefill_for('g', [{'name': 'topic'}])
    assert read == ['r0']
    assert prefill
