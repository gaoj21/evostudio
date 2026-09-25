"""Regressions in the execution layer: batches across restarts and stops,
comparing streamed batches, and runs that outlive their usefulness in memory.
"""

import json

import pytest


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


@pytest.fixture
def engine(studio_data, monkeypatch):
    """The real run engine with a model that answers at once."""
    from evoagentx.models import LiteLLMConfig
    from backend.api import runner, tools_registry
    seen: list[dict] = []

    async def fake_llm(agent, task, inputs, state):
        seen.append(state)
        return {o["name"]: f"out-{inputs}" for o in task.get("outputs") or []}

    class StubLLM:
        config = LiteLLMConfig(model="deepseek/deepseek-chat", deepseek_key="test-only")

    monkeypatch.setattr(runner, "execute_llm_node", fake_llm)
    monkeypatch.setattr(runner, "_make_llm", lambda **kw: StubLLM())
    monkeypatch.setattr(runner, "_prepare_ltm", lambda doc, ordered, inputs, state: ({}, ordered))
    monkeypatch.setattr(runner, "_attach_ltm", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_save_ltm", lambda *a, **k: None)
    monkeypatch.setattr(tools_registry, "validate_tool_names", lambda names: None)
    monkeypatch.setattr(tools_registry, "resolve_tools", lambda names, **kw: None)
    runner._runs.clear()
    return seen


def _graph():
    from conftest import make_graph, make_task
    return make_graph([make_task("a", inputs=["text"], outputs=["x"])], id="fixes")


# --- a stop the process never finished --------------------------------------

def test_a_batch_left_cancelling_by_a_restart_reads_cancelled_and_resumes(batches, monkeypatch):
    items = [{"index": i, "status": st, "run_id": f"r{i}" if st != "pending" else None,
              "inputs": {"text": str(i)}, "output_summary": None, "error": None,
              "review_status": None, "label": None, "score": None, "score_detail": None}
             for i, st in enumerate(["success", "running", "pending"])]
    batches._persist_batch({"batch_id": "b1", "graph_id": "g1", "status": "cancelling",
                            "cancel_requested": True, "workers": 1, "total": 3,
                            "items": items, "created_at": "2026-01-01T00:00:00+00:00"})

    assert batches.mark_interrupted() == 1
    saved = batches._read_batch("b1")
    assert saved["status"] == "cancelled"
    assert [i["status"] for i in saved["items"]] == ["success", "failed", "cancelled"]
    assert batches.cancel_batch("b1")["reason"] == "batch already cancelled"

    ran = []
    monkeypatch.setattr(batches.runner, "start_run",
                        lambda graph, record, **kw: ran.append(record["text"]))
    monkeypatch.setattr(batches.runner, "get_run", lambda rid: {"status": "success", "result": {}})
    outcome = batches.resume_batch("b1", {"id": "g1"})
    assert outcome["resumed"] is True and outcome["remaining"] == 2
    assert batches.wait_for("b1", timeout=10)
    assert ran == ["1", "2"]


# --- a stream that dies in its first chunk ----------------------------------

def test_a_streaming_batch_is_marked_streaming_in_its_first_write(batches, monkeypatch):
    from backend.features.execution import stream_batch
    writes = []
    monkeypatch.setattr(batches, "_persist_batch", lambda state: writes.append(dict(state)))
    monkeypatch.setattr(stream_batch, "execute_stream", lambda *a: None)

    batches.start_batch({"id": "g1"}, [], {"type": "manual"}, workers=1,
                        record_chunks=lambda cancelled: iter(()))

    assert writes[0]["streaming"] is True
    assert writes[0]["collection_complete"] is False


# --- comparing streamed batches ---------------------------------------------

def _streamed(batch_id, records, scores):
    """Items as a streamed batch leaves them: inputs archived, then trimmed."""
    from backend.features.execution import stream_batch
    state = {"items": []}
    pairs = stream_batch._archive(state, batch_id, records, None)
    for (item, _), score in zip(pairs, scores):
        item.update(status="success", score=score)
    stream_batch._release(pairs)
    return {"batch_id": batch_id, "metric": "m", "items": state["items"]}


def test_streamed_records_that_differ_only_in_long_fields_stay_distinct(batches):
    from backend.features.execution import batch_compare
    records = [{"key": "same", "text": "A" * 200}, {"key": "same", "text": "B" * 200},
               {"key": "other", "text": "C" * 200}]
    base = _streamed("base", records, [1.0, 0.0, 1.0])
    cand = _streamed("cand", records, [0.0, 1.0, 1.0])
    # What the trimmed inputs alone would have collapsed.
    assert base["items"][0]["inputs"] == base["items"][1]["inputs"]

    result = batch_compare.compare(base, cand)
    assert result["matched"] == 3
    assert (result["improved"], result["regressed"], result["unchanged"]) == (1, 1, 1)
    assert not any("duplicate" in note for note in result["notes"])


def test_means_cover_only_records_scored_on_both_sides():
    from backend.features.execution import batch_compare
    base = {"items": [{"inputs": {"q": 1}, "score": 0.0}, {"inputs": {"q": 2}, "score": 1.0}]}
    cand = {"items": [{"inputs": {"q": 1}, "score": None, "status": "failed"},
                      {"inputs": {"q": 2}, "score": 1.0}]}

    result = batch_compare.compare(base, cand)
    assert result["mean_before"] == 1.0
    assert result["mean_after"] == 1.0
    assert result["delta"] == 0.0
    assert result["scored_both"] == 1
    assert result["unscored"] == 1


# --- settled runs leave memory ----------------------------------------------

def test_a_settled_run_is_dropped_from_memory_and_read_from_disk(engine):
    from backend.api import runner
    run_id = runner.start_run(_graph(), {"text": "t"}, background=False)

    assert run_id not in runner._runs
    run = runner.get_run(run_id)
    assert run["status"] == "success"
    assert run["result"] == {"x": "out-{'text': 't'}"}
    # The state the engine held is emptied of its live handles too.
    assert not [k for k in engine[0] if k.startswith("_")]
    assert runner.cancel_run(run_id) == {"cancelled": False, "reason": "run already success"}


def test_a_batch_still_reads_its_items_once_their_runs_are_dropped(engine, batches):
    from backend.api import runner
    batch_id = batches.start_batch(_graph(), [{"text": str(i)} for i in range(5)],
                                   {"type": "manual"}, workers=2)
    assert batches.wait_for(batch_id, timeout=20)

    state = batches.get_batch(batch_id)
    assert state["status"] == "succeeded"
    assert all(i["output_summary"] for i in state["items"])
    assert not any(i["run_id"] in runner._runs for i in state["items"])
    assert state["node_progress"] == {"a": {"completed": 5, "running": 0, "failed": 0, "pending": 0}}


# --- stopping something that just settled -----------------------------------

def test_a_stop_that_arrives_after_the_loop_closed_is_harmless(engine, monkeypatch):
    from backend.api import runner
    grabbed = []

    async def grab(agent, task, inputs, state):
        grabbed.append(state["_cancel"])            # what cancel_run holds, outside the lock
        return {"x": "done"}

    monkeypatch.setattr(runner, "execute_llm_node", grab)
    runner.start_run(_graph(), {"text": "t"}, background=False)
    grabbed[0]()                                    # the loop is closed by now; must not raise

    runner._runs["late"] = {"run_id": "late", "status": "running",
                            "_cancel": lambda: (_ for _ in ()).throw(RuntimeError("closed"))}
    assert runner.cancel_run("late") == {"cancelled": True}


def test_a_batch_stop_reaches_every_run_even_if_one_fails_to_stop(batches, monkeypatch):
    called = []

    def cancel_run(run_id, *, expected=False):
        called.append(run_id)
        if run_id == "r0":
            raise RuntimeError("Event loop is closed")
        return {"cancelled": True}

    monkeypatch.setattr(batches.runner, "cancel_run", cancel_run)
    items = [{"index": i, "status": "running", "run_id": f"r{i}", "inputs": {}} for i in range(3)]
    batches._batches["b1"] = {"batch_id": "b1", "status": "running", "items": items,
                              "cancelled_items": 0}

    outcome = batches.cancel_batch("b1")
    assert outcome["cancelled"] is True
    assert called == ["r0", "r1", "r2"]
    assert json.loads((batches.BATCHES_DIR / "b1.json").read_text())["status"] == "cancelling"
