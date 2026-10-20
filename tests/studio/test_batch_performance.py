"""Watching a batch does not slow it down.

Node by node, a chunk's records run with one model call each in flight by
default rather than two for the whole chunk; a poll counts progress from the
batch's own items instead of reading every settled Run back from disk, and
never reads a Run while holding the batch lock; the stage is reported outside
the gate's lock; a whole-file save is written outside the batch lock; and the
page's poll leaves each item's input record out.
"""

import json
import threading

import pytest

from test_node_by_node import batches, calls, three_nodes  # noqa: F401  (fixtures)


# --- how many model calls a chunk has in flight --------------------------------

def test_node_by_node_defaults_to_one_call_per_record_up_to_the_cap(batches, calls):
    records = [{"id": f"r{i}"} for i in range(6)]
    batch_id = batches.start_batch(three_nodes(), records, {"type": "upload"}, mode="node")
    assert batches.wait_for(batch_id, timeout=20)
    assert batches.get_batch(batch_id)["workers"] == 6

    source = {"type": "canvas", "config": {"type": "dataloader", "read_batch_size": 500}}
    assert batches.default_workers("node", 500) == batches.DEFAULT_CONCURRENCY
    batch_id = batches.start_batch(three_nodes(), [], source, mode="node",
                                   record_chunks=lambda cancelled, on_info=None: (c for c in ()))
    assert batches.get_batch(batch_id)["workers"] == batches.DEFAULT_CONCURRENCY
    batches.wait_for(batch_id, timeout=20)


def test_record_by_record_keeps_its_default_and_an_explicit_number_wins(batches, calls):
    records = [{"id": f"r{i}"} for i in range(6)]
    batch_id = batches.start_batch(three_nodes(), records, {"type": "upload"}, mode="record")
    assert batches.wait_for(batch_id, timeout=20)
    assert batches.get_batch(batch_id)["workers"] == batches.RECORD_MODE_WORKERS

    batch_id = batches.start_batch(three_nodes(), records, {"type": "upload"}, mode="node", workers=3)
    assert batches.wait_for(batch_id, timeout=20)
    assert batches.get_batch(batch_id)["workers"] == 3


def test_a_requested_worker_count_is_checked(batches):
    assert batches.requested_workers(None) is None
    assert batches.requested_workers("") is None
    assert batches.requested_workers("12") == 12
    for bad in (0, batches.MAX_WORKERS + 1, "x", True):
        with pytest.raises(ValueError):
            batches.requested_workers(bad)


# --- progress from the batch alone ----------------------------------------------

def test_progress_of_settled_items_never_reads_their_runs(batches, calls, monkeypatch):
    from backend.api import runner
    records = [{"id": f"r{i}"} for i in range(4)]
    batch_id = batches.start_batch(three_nodes(), records, {"type": "upload"}, mode="node")
    assert batches.wait_for(batch_id, timeout=20)

    def refuse(run_id):
        raise AssertionError("a settled item's Run was read back")
    monkeypatch.setattr(runner, "get_run", refuse)
    monkeypatch.setattr(runner, "_read_run", refuse)

    live = batches.get_batch(batch_id)
    expected = {"completed": 4, "running": 0, "failed": 0, "pending": 0}
    assert live["node_progress"] == {"a": expected, "b": expected, "c": expected}
    # The same from the saved file, once the process has let the batch go.
    batches._batches.clear()
    assert batches.get_batch(batch_id)["node_progress"]["c"] == expected


def test_a_poll_reads_running_runs_outside_the_batch_lock(batches, monkeypatch):
    from backend.api import runner
    state = {"batch_id": "b1", "graph_id": "g", "status": "running", "created_at": "t",
             "items": [{"index": 0, "status": "running", "run_id": "r0", "attempt_started_at": ""},
                       {"index": 1, "status": "success", "run_id": "r1",
                        "node_status": {"a": "completed"}, "node_usage": {}}]}
    batches._batches["b1"] = state
    held = []

    def get_run(run_id):
        held.append(batches._lock.locked())
        return {"created_at": "x", "nodes": [{"name": "a", "status": "running"}],
                "token_usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5, "reported_calls": 1}}
    monkeypatch.setattr(runner, "get_run", get_run)

    view = batches.get_batch("b1")
    assert view["node_progress"]["a"] == {"completed": 1, "running": 1, "failed": 0, "pending": 0}
    assert view["token_usage"]["total_tokens"] == 5
    assert held and not any(held)
    # The live view is computed, never written into the batch itself.
    assert "token_usage" not in state


# --- the gate reports its stage outside its own lock ----------------------------

def test_the_stage_is_reported_outside_the_gate_lock():
    from backend.features.execution.node_gate import NodeGate
    free = []

    def on_stage(stage):
        # Another thread can take the gate's lock while the stage is reported.
        got = []

        def probe():
            taken = gate._cv.acquire(timeout=1)
            if taken:
                gate._cv.release()
            got.append(taken)
        thread = threading.Thread(target=probe)
        thread.start()
        thread.join()
        free.append(got[0])

    gate = NodeGate(["a", "b"], workers=2, on_stage=on_stage)
    gate.expect("r0")
    gate.wait_turn("r0", 0)
    gate.done("r0", 0)
    gate.leave("r0")
    assert free and all(free)


# --- saving --------------------------------------------------------------------

def _state(n=3):
    return {'batch_id': 'b1', 'graph_id': 'g', 'status': 'running', 'created_at': 't',
            'items': [{'index': i, 'status': 'pending', 'run_id': None} for i in range(n)]}


def test_a_whole_file_save_is_written_outside_the_lock_and_keeps_later_lines(batches, monkeypatch):
    s = _state()
    batches._persist_batch(s)
    s['items'][0].update(status='success', run_id='r0')
    with batches._lock:
        assert batches._persist_item(s, s['items'][0]) is None      # a journal line
    s['_snapshot_at'] -= batches.SNAPSHOT_SECONDS + 1
    s['items'][1].update(status='success', run_id='r1')
    with batches._lock:
        snapshot = batches._persist_item(s, s['items'][1])          # the copy, under the lock
    assert snapshot is not None
    # A record settling between the copy and the write is journaled ...
    s['items'][2].update(status='failed', run_id='r2')
    with batches._lock:
        assert batches._persist_item(s, s['items'][2]) is None

    written = batches._write_temporary
    held = []

    def watching(path, data):
        held.append(batches._lock.locked())
        return written(path, data)
    monkeypatch.setattr(batches, "_write_temporary", watching)
    batches._write_snapshot(snapshot)
    assert held == [False]

    # ... and survives the snapshot that did not include it.
    lines = (batches.BATCHES_DIR / 'b1.items.jsonl').read_text().splitlines()
    assert [json.loads(line)['index'] for line in lines] == [2]
    read = batches._read_batch('b1')
    assert [i['status'] for i in read['items']] == ['success', 'success', 'failed']


def test_an_older_snapshot_never_overwrites_a_newer_save(batches):
    s = _state()
    batches._persist_batch(s)
    s['_snapshot_at'] = 0.0
    s['items'][0]['status'] = 'running'
    with batches._lock:
        stale = batches._persist_item(s, s['items'][0])
    s['items'][0]['status'] = 'success'
    batches._persist_batch(s)                                       # newer, and on disk first
    batches._write_snapshot(stale)
    assert batches._read_batch('b1')['items'][0]['status'] == 'success'


# --- what the page polls ---------------------------------------------------------

def test_the_polled_batch_leaves_the_input_records_out(batches, calls):
    from fastapi.testclient import TestClient
    from backend.api.app import app
    records = [{"id": f"r{i}", "text": "x" * 1000} for i in range(3)]
    batch_id = batches.start_batch(three_nodes(), records, {"type": "upload"}, mode="node")
    assert batches.wait_for(batch_id, timeout=20)
    with TestClient(app) as client:
        polled = client.get(f"/api/batches/{batch_id}").json()
        assert polled["items"] and all("inputs" not in i for i in polled["items"])
        assert all(i["status"] == "success" for i in polled["items"])
        full = client.get(f"/api/batches/{batch_id}", params={"inputs": True}).json()
        assert full["items"][0]["inputs"]["id"] == "r0"
    # Everything that reads a batch in full still has them.
    assert batches.get_batch(batch_id)["items"][0]["inputs"]["id"] == "r0"
