"""A batch's records live beside it, not inside it.

Every item used to carry its whole record, and a batch of large records made
a batch file of tens of megabytes — rewritten every few seconds while it ran.
The records now go to `<batch_id>.inputs.jsonl` once, and everything that
needs them (resume, retry passes, `?inputs=true`, export, compare) reads
them back from there.
"""
import json

import pytest

BIG = "x" * 5000


@pytest.fixture
def batches(tmp_path, monkeypatch):
    from backend.api import batch as batch_module
    monkeypatch.setattr(batch_module, "BATCHES_DIR", tmp_path / "batches")
    monkeypatch.setattr(batch_module, "_pause", lambda seconds, batch_id=None: None)
    batch_module._batches.clear()
    batch_module._threads.clear()
    yield batch_module
    for batch_id in list(batch_module._threads):
        assert batch_module.wait_for(batch_id, timeout=10)


class Recorder:
    def __init__(self, outcome=None):
        self.calls, self.runs = [], {}
        self.outcome = outcome or (lambda record, n: {"status": "success", "result": {"id": record["id"]}})

    def start_run(self, graph, record, background, gray_zone, run_id, batch_id, session_started_at, **_):
        self.calls.append(dict(record))
        n = sum(1 for c in self.calls if c["id"] == record["id"])
        self.runs[run_id] = dict(self.outcome(record, n))

    def get_run(self, run_id):
        return self.runs.get(run_id)


def records(count=40):
    return [{"id": f"r{i}", "text": f"{i} {BIG}", "nested": {"deep": [i] * 50}} for i in range(count)]


def graph():
    return {"id": "g1", "tasks": [], "edges": []}


def wire(batches, monkeypatch, rec):
    monkeypatch.setattr(batches.runner, "start_run", rec.start_run)
    monkeypatch.setattr(batches.runner, "get_run", rec.get_run)


def test_the_batch_file_stays_small_and_the_records_are_read_back(batches, monkeypatch):
    rec = Recorder()
    wire(batches, monkeypatch, rec)
    rows = records()
    batch_id = batches.start_batch(graph(), rows, {"type": "upload"}, workers=4)
    assert batches.wait_for(batch_id, timeout=20)

    # Every run was handed its whole record.
    assert sorted(c["id"] for c in rec.calls) == sorted(r["id"] for r in rows)
    assert all(c["text"].endswith(BIG) and len(c["nested"]["deep"]) == 50 for c in rec.calls)

    path = batches.BATCHES_DIR / f"{batch_id}.json"
    inline = len(json.dumps(rows))
    assert path.stat().st_size < inline / 10, (path.stat().st_size, inline)
    saved = json.loads(path.read_text())
    assert all(i["inputs"] == {"id": i["inputs"]["id"]} and i["inputs_archived"] for i in saved["items"])
    assert (batches.BATCHES_DIR / f"{batch_id}.inputs.jsonl").is_file()

    # In full, from memory and from disk alike.
    full = batches.get_batch(batch_id)
    assert [i["inputs"] for i in full["items"]] == rows
    batches._batches.clear()
    assert [i["inputs"] for i in batches.get_batch(batch_id)["items"]] == rows
    # Not read back when the caller says it does not need them.
    assert all("text" not in i["inputs"] for i in batches.get_batch(batch_id, inputs=False)["items"])
    assert all("inputs" not in i and "input_at" not in i for i in batches.batch_view(batch_id)["items"])


def test_small_records_stay_inline_and_write_no_inputs_file(batches, monkeypatch):
    wire(batches, monkeypatch, Recorder())
    rows = [{"id": f"r{i}", "n": i} for i in range(3)]
    batch_id = batches.start_batch(graph(), rows, {"type": "upload"})
    assert batches.wait_for(batch_id, timeout=20)
    assert not (batches.BATCHES_DIR / f"{batch_id}.inputs.jsonl").exists()
    saved = json.loads((batches.BATCHES_DIR / f"{batch_id}.json").read_text())
    assert [i["inputs"] for i in saved["items"]] == rows


def test_a_retry_pass_runs_on_the_whole_record(batches, monkeypatch):
    def outcome(record, n):
        if record["id"] == "r1" and n == 1:
            return {"status": "failed", "error": "unreachable", "node_error": {"retryable": True}}
        return {"status": "success", "result": {}}
    rec = Recorder(outcome)
    wire(batches, monkeypatch, rec)
    batch_id = batches.start_batch(graph(), records(3), {"type": "upload"}, workers=1)
    assert batches.wait_for(batch_id, timeout=20)
    again = [c for c in rec.calls if c["id"] == "r1"]
    assert len(again) == 2 and again[1]["text"].endswith(BIG)
    assert batches.get_batch(batch_id)["status"] == "succeeded"


def test_resume_runs_on_the_whole_record_after_a_restart(batches, monkeypatch):
    rec = Recorder(lambda record, n: {"status": "failed", "error": "no"} if record["id"] == "r2"
                   else {"status": "success", "result": {}})
    wire(batches, monkeypatch, rec)
    rows = records(4)
    batch_id = batches.start_batch(graph(), rows, {"type": "upload"}, workers=1)
    assert batches.wait_for(batch_id, timeout=20)
    batches._batches.clear()                      # only on disk now, as after a restart
    batches._threads.clear()

    later = Recorder()
    wire(batches, monkeypatch, later)
    outcome = batches.resume_batch(batch_id, graph())
    assert outcome["resumed"] and outcome["remaining"] == 1
    assert batches.wait_for(batch_id, timeout=20)
    assert later.calls == [rows[2]]
    done = batches.get_batch(batch_id)
    assert done["status"] == "succeeded"
    assert [i["inputs"] for i in done["items"]] == rows


def test_a_batch_saved_with_its_records_inline_still_reads_and_resumes(batches, monkeypatch):
    rows = records(3)
    items = [{"index": i, "status": "success" if i < 2 else "cancelled", "run_id": f"old{i}" if i < 2 else None,
              "inputs": row, "output_summary": None, "error": None, "review_status": None,
              "label": None, "score": None, "score_detail": None}
             for i, row in enumerate(rows)]
    batches._persist_batch({"batch_id": "old", "graph_id": "g1", "status": "cancelled",
                            "created_at": "2026-01-01T00:00:00+00:00", "source": {"type": "upload"},
                            "metric": None, "summary": None, "workers": 1, "total": 3,
                            "items": items, "cancelled_items": 1})
    assert [i["inputs"] for i in batches.get_batch("old")["items"]] == rows

    rec = Recorder()
    wire(batches, monkeypatch, rec)
    assert batches.resume_batch("old", graph())["resumed"]
    assert batches.wait_for("old", timeout=20)
    assert rec.calls == [rows[2]]
    assert [i["inputs"] for i in batches.get_batch("old")["items"]] == rows


def test_export_and_compare_see_the_whole_record(batches, monkeypatch):
    from backend.features.execution import batch_compare, batch_export
    wire(batches, monkeypatch, Recorder())
    rows = records(3)
    first = batches.start_batch(graph(), rows, {"type": "upload"})
    assert batches.wait_for(first, timeout=20)
    second = batches.start_batch(graph(), rows, {"type": "upload"})
    assert batches.wait_for(second, timeout=20)

    columns, table = batch_export.to_rows(batches.get_batch(first))
    assert "input_text" in columns and table[0]["input_text"] == rows[0]["text"]
    # Straight from the file, without get_batch having read them back.
    columns, table = batch_export.to_rows(batches.get_batch(first, inputs=False))
    assert table[2]["input_text"] == rows[2]["text"]

    compared = batch_compare.compare(batches.get_batch(first), batches.get_batch(second))
    # Records are told apart by their whole inputs: all three pair up.
    assert sorted(c["inputs"]["id"] for c in compared["changes"]) == ["r0", "r1", "r2"]
    assert all(c["inputs"]["text"].endswith(BIG) for c in compared["changes"])
