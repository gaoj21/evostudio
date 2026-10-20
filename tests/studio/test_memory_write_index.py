"""A subject-tracked store is not read back whole on every write and recall.

Each write folds a run into its subject's entry, and used to find that entry
by reading every entry of the store from disk (memory_store.list_entries);
recall did the same on every node run. The cost grew with the store, and a
batch's with its square. Now one read builds an index that writes keep up to
date, and recall's copy is kept while the store on disk is unchanged.

The write index also covers what the shared store holds but has not saved:
two records of one subject in the same chunk used to each read only disk,
and left two partial entries for the subject instead of one.
"""
import json
import shutil
import threading

import pytest

TASK = {"name": "judge", "description": "d",
        "memory": {"kind": "recall", "match": "company", "at": "as_of", "retrieve": 5,
                   "context": ["as_of"], "inputs": ["company"]}}


@pytest.fixture
def store(tmp_path, monkeypatch):
    from backend.api import memory_store, runner
    monkeypatch.setattr(memory_store, "MEMORY_DIR", tmp_path / "memory")
    # Long enough that every record started together is in the one store
    # before the last of them saves it.
    monkeypatch.setattr(runner, "SAVE_GRACE_SECONDS", 1.0)
    runner._saved_indexes.clear()
    yield memory_store
    runner._saved_indexes.clear()


@pytest.fixture
def reads(store, monkeypatch):
    """How many times the whole store was read back."""
    count = []
    real = store.list_entries
    monkeypatch.setattr(store, "list_entries", lambda *a, **kw: count.append(1) or real(*a, **kw))
    return count


def payload(company, as_of, verdict):
    return {"task": "judge", "inputs": {"company": company, "as_of": as_of},
            "outputs": {"verdict": verdict}}


def as_message(body):
    from evoagentx.core.message import Message, MessageType
    return Message(content=json.dumps(body, ensure_ascii=False, default=str),
                   msg_type=MessageType.RESPONSE, agent="judge",
                   wf_goal="g", wf_task="judge", wf_task_desc="d")


def write_chunk(writes, graph_id="g1"):
    """`writes` made as the records of one chunk make them: side by side,
    into one shared store, saved once by the last of them to leave."""
    from backend.api import runner
    start, errors = threading.Barrier(len(writes)), []

    def record(write):
        start.wait()
        try:
            runner._write_shared(graph_id, "judge", {}, lambda memory, entries: runner._write_entry(
                memory, graph_id, "judge", TASK, payload(*write), as_message, entries))
        except Exception as error:          # pragma: no cover - reported below
            errors.append(error)

    threads = [threading.Thread(target=record, args=(w,)) for w in writes]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert not errors, errors


def timelines(store, graph_id="g1"):
    return {json.loads(e["content"])["subject"]["company"]: [p["at"] for p in json.loads(e["content"])["timeline"]]
            for e in store.list_entries(graph_id, "judge")}


def test_one_subject_twice_in_one_chunk_is_one_entry_with_both(store):
    write_chunk([("Acme", "2026-01-01", "a"), ("Acme", "2026-02-01", "b"), ("Wayfair", "2026-01-01", "c")])
    entries = store.list_entries("g1", "judge")
    assert len(entries) == 2, [json.loads(e["content"])["timeline"] for e in entries]
    assert sorted(timelines(store)["Acme"]) == ["2026-01-01", "2026-02-01"]


def test_a_chunk_reads_the_store_once_however_many_records_it_writes(store, reads):
    write_chunk([("Seed", "2025-12-01", "s")])
    reads.clear()
    write_chunk([(f"co{i}", "2026-01-01", "v") for i in range(8)])
    assert len(reads) <= 1, f"{len(reads)} whole-store reads for one chunk of 8 writes"
    assert len(timelines(store)) == 9


def test_writes_across_chunks_still_build_on_what_was_saved(store):
    write_chunk([("Acme", "2026-01-01", "a")])
    write_chunk([("Acme", "2026-02-01", "b")])
    write_chunk([("Acme", "2026-03-01", "c"), ("Acme", "2026-01-01", "a2")])
    entries = store.list_entries("g1", "judge")
    assert len(entries) == 1
    body = json.loads(entries[0]["content"])
    assert [p["at"] for p in body["timeline"]] == ["2026-01-01", "2026-02-01", "2026-03-01"]
    # Re-running a date replaces that date.
    assert next(p for p in body["timeline"] if p["at"] == "2026-01-01")["outputs"]["verdict"] == "a2"


def test_the_write_path_without_an_index_is_unchanged(store):
    from backend.api import runner
    for month in (1, 2):
        with runner._ltm_lock("g1/judge"):
            memory = store.open_memory("g1", "judge", create=True)
            runner._write_entry(memory, "g1", "judge", TASK, payload("Acme", f"2026-0{month}-01", "v"), as_message)
            memory.save()
            store.close_memory(memory)
    assert timelines(store) == {"Acme": ["2026-01-01", "2026-02-01"]}


def test_recall_reads_the_store_once_while_it_is_unchanged(store, reads):
    from backend.api import memory_policy, runner
    write_chunk([("Acme", "2026-01-01", "a"), ("Beta", "2026-01-01", "b")])
    history = runner._History("g1")
    reads.clear()
    first = [history.about("judge", TASK, "Acme") for _ in range(5)]
    assert len(reads) == 1
    assert all(len(found) == 1 for found in first)

    # Saved since: read again, and the new state is there.
    write_chunk([("Acme", "2026-02-01", "a2")])
    reads.clear()
    block = memory_policy.history_for(history.about("judge", TASK, "Acme"), TASK,
                                      {"company": "Acme", "as_of": "2026-03-01"})
    assert len(reads) == 1
    assert "2026-01-01" in block and "2026-02-01" in block


def test_recall_through_the_index_says_what_the_whole_store_says(store):
    from backend.api import memory_policy, runner
    write_chunk([("Acme", f"2026-0{m}-01", f"v{m}") for m in (1, 3, 6)]
                + [("Beta", "2026-02-01", "b")])
    for inputs in ({"company": "Acme", "as_of": "2026-05-01"}, {"company": "Acme", "as_of": "2026-01-01"},
                   {"company": "Beta", "as_of": "2027-01-01"}, {"company": "Nobody", "as_of": "2027-01-01"}):
        indexed = memory_policy.history_for(
            runner._History("g1").about("judge", TASK, inputs["company"]), TASK, inputs)
        whole = memory_policy.history_for(store.list_entries("g1", "judge"), TASK, inputs)
        assert indexed == whole, inputs
    # Strictly before the run's date: May cannot see June, and January sees nothing.
    may = memory_policy.history_for(runner._History("g1").about("judge", TASK, "Acme"), TASK,
                                    {"company": "Acme", "as_of": "2026-05-01"})
    assert "v1" in may and "v3" in may and "v6" not in may
    assert memory_policy.history_for(runner._History("g1").about("judge", TASK, "Acme"), TASK,
                                     {"company": "Acme", "as_of": "2026-01-01"}) == ""


def test_recall_async_looks_the_subject_up(store):
    import asyncio
    from backend.api import memory_policy, runner
    write_chunk([("Acme", "2026-01-01", "a"), ("Beta", "2026-01-01", "b")])
    asked = []

    class History(runner._History):
        def about(self, name, task, subject):
            asked.append(subject)
            return super().about(name, task, subject)

    inputs = {"company": "Acme", "as_of": "2026-06-01"}
    via_index = asyncio.run(memory_policy.recall_async({}, TASK, inputs, history=History("g1")))
    whole = asyncio.run(memory_policy.recall_async(
        {}, TASK, inputs, history=lambda name: store.list_entries("g1", name)))
    assert asked == ["Acme"]
    assert via_index == whole and "2026-01-01" in via_index


def test_a_store_removed_and_started_again_is_read_afresh(store):
    """Each Evolve replay remembers from empty: an index of an earlier store
    of the same name must never answer for the new one."""
    from backend.api import runner
    write_chunk([("Acme", "2026-01-01", "old")])
    assert runner._History("g1").about("judge", TASK, "Acme")
    shutil.rmtree(store.store_dir("g1", "judge"))
    assert runner._History("g1").about("judge", TASK, "Acme") == []
    write_chunk([("Beta", "2026-01-01", "new")])
    assert runner._History("g1").about("judge", TASK, "Acme") == []
    assert len(runner._History("g1").about("judge", TASK, "Beta")) == 1


def test_unsaved_writes_are_not_recalled(store):
    """Recall reads what is saved: records of one chunk run side by side,
    and none of them is "before" another."""
    from backend.api import runner
    write_chunk([("Acme", "2026-01-01", "a")])
    with runner._ltm_lock("g1/judge"):
        memory = store.open_memory("g1", "judge", create=True)
        entries = runner._LazyEntries("g1", "judge")
        runner._write_entry(memory, "g1", "judge", TASK, payload("Acme", "2026-02-01", "b"), as_message, entries)
        # The write's own index has it ...
        found = entries.built.about(TASK, "Acme")
        assert [p["at"] for p in found[0][1]["timeline"]] == ["2026-01-01", "2026-02-01"]
        store.close_memory(memory)           # ... never saved
    recalled = runner._History("g1").about("judge", TASK, "Acme")
    assert [p["at"] for p in json.loads(recalled[0]["content"])["timeline"]] == ["2026-01-01"]
