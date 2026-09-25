"""Listing runs and batches reads each file once per version of it: fast on
the hundredth call, and never stale after the file changes."""
import json


def test_a_batch_listing_follows_the_file(tmp_path, monkeypatch):
    from backend.api import batch
    monkeypatch.setattr(batch, 'BATCHES_DIR', tmp_path)
    batch._batches.clear()
    path = tmp_path / 'b1.json'
    path.write_text(json.dumps({'batch_id': 'b1', 'graph_id': 'g', 'status': 'running', 'items': []}))
    assert batch.list_batches('g')[0]['status'] == 'running'
    path.write_text(json.dumps({'batch_id': 'b1', 'graph_id': 'g', 'status': 'succeeded', 'items': [{'status': 'success'}]}))
    listed = batch.list_batches('g')[0]
    assert listed['status'] == 'succeeded' and listed['counts'] == {'success': 1}
    path.unlink()
    assert batch.list_batches('g') == []


def test_runs_are_chosen_on_a_small_view_and_read_in_full_only_when_returned(tmp_path, monkeypatch):
    from backend.api import runner
    monkeypatch.setattr(runner, 'RUNS_DIR', tmp_path)
    runner._runs.clear()
    for i in range(30):
        (tmp_path / f'r{i:02d}.json').write_text(json.dumps(
            {'run_id': f'r{i:02d}', 'graph_id': 'g' if i % 2 else 'other', 'created_at': f'2026-01-{i + 1:02d}',
             'status': 'success', 'result': 'x' * 1000}))
    read = []
    real = runner._read_run
    monkeypatch.setattr(runner, '_read_run', lambda run_id: read.append(run_id) or real(run_id))

    runs = runner.list_runs('g', limit=3)

    assert [r['run_id'] for r in runs] == ['r29', 'r27', 'r25']
    assert sorted(read) == ['r25', 'r27', 'r29']            # only what is returned
    (tmp_path / 'r31.json').write_text(json.dumps({'run_id': 'r31', 'graph_id': 'g', 'created_at': '2026-02-01'}))
    assert runner.list_runs('g', limit=1)[0]['run_id'] == 'r31'
