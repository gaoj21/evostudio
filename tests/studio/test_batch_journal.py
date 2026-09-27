"""A long batch does not spend more time saving itself the longer it runs:
each record's change is one journal line, the whole file is written at most
every few seconds, and whoever reads it from disk gets the journal applied."""
import json

import pytest


@pytest.fixture
def batches(tmp_path, monkeypatch):
    from backend.api import batch
    monkeypatch.setattr(batch, 'BATCHES_DIR', tmp_path)
    batch._batches.clear()
    return batch


def state(n=3):
    return {'batch_id': 'b1', 'graph_id': 'g', 'status': 'running', 'created_at': 't',
            'items': [{'index': i, 'status': 'pending', 'run_id': None} for i in range(n)]}


def test_a_record_change_is_a_journal_line_not_a_rewrite(batches, tmp_path):
    s = state()
    batches._persist_batch(s)                              # the snapshot, at the start
    written = (tmp_path / 'b1.json').stat().st_mtime_ns
    s['items'][1].update(status='running', run_id='r1', attempt_started_at='a')
    batches._persist_item(s, s['items'][1])
    s['items'][1].update(status='success')
    batches._persist_item(s, s['items'][1])

    assert (tmp_path / 'b1.json').stat().st_mtime_ns == written          # not rewritten
    lines = (tmp_path / 'b1.items.jsonl').read_text().splitlines()
    assert [json.loads(l)['status'] for l in lines] == ['running', 'success']
    # Read back from disk — History, Resume, a restart — the journal is applied.
    read = batches._read_batch('b1')
    assert read['items'][1]['status'] == 'success' and read['items'][1]['run_id'] == 'r1'
    assert read['items'][0]['status'] == 'pending'


def test_the_whole_file_every_few_seconds_supersedes_the_journal(batches, tmp_path, monkeypatch):
    s = state()
    batches._persist_batch(s)
    s['items'][0].update(status='success', run_id='r0')
    batches._persist_item(s, s['items'][0])
    assert (tmp_path / 'b1.items.jsonl').is_file()
    s['_snapshot_at'] -= batches.SNAPSHOT_SECONDS + 1            # time for the next snapshot
    s['items'][2].update(status='failed', run_id='r2')
    batches._persist_item(s, s['items'][2])
    assert not (tmp_path / 'b1.items.jsonl').exists()
    on_disk = json.loads((tmp_path / 'b1.json').read_text())
    assert [i['status'] for i in on_disk['items']] == ['success', 'pending', 'failed']
    assert '_snapshot_at' not in on_disk


def test_a_restart_reads_the_journal_before_marking_the_batch_interrupted(batches, tmp_path):
    s = state()
    batches._persist_batch(s)
    s['items'][0].update(status='success', run_id='r0')
    batches._persist_item(s, s['items'][0])
    s['items'][1].update(status='running', run_id='r1')
    batches._persist_item(s, s['items'][1])
    (tmp_path / 'b1.items.jsonl').open('a').write('{"index": 2, "sta')     # cut off by the crash

    assert batches.mark_interrupted() == 1
    data = json.loads((tmp_path / 'b1.json').read_text())
    assert data['status'] == 'interrupted'
    assert [i['status'] for i in data['items']] == ['success', 'failed', 'failed']
    assert data['items'][1]['run_id'] == 'r1'                    # recovery can reconcile it
    assert not (tmp_path / 'b1.items.jsonl').exists()
