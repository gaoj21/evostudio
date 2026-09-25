"""A batch runs each chunk node by node.

Every record of a chunk starts together and they keep step: no record begins
a node before every record still running has finished the one before it. So
the chunk moves through the workflow one node at a time — what the canvas
shows — and a node's calls arrive together. Chunks run one after another, so
what one writes to memory is there before the next begins.
"""

import random
import threading
import time

import pytest

from conftest import make_graph, make_task


@pytest.fixture
def batches(tmp_path, monkeypatch):
    from backend.api import batch as batch_module
    monkeypatch.setattr(batch_module, "BATCHES_DIR", tmp_path / "batches")
    monkeypatch.setattr(batch_module, "_pause", lambda seconds, batch_id=None: None)
    batch_module._batches.clear()
    batch_module._threads.clear()
    yield batch_module
    for batch_id in list(batch_module._threads):
        assert batch_module.wait_for(batch_id, timeout=20)


@pytest.fixture
def calls(studio_data, monkeypatch):
    """The real engine, with a model that answers after a random moment and
    logs (record, node) in the order the calls actually happened."""
    from evoagentx.models import LiteLLMConfig
    from backend.api import runner, tools_registry
    log: list[tuple[str, str]] = []
    lock = threading.Lock()
    failing: set[tuple[str, str]] = set()

    async def fake_llm(agent, task, inputs, state):
        import asyncio
        record = state["inputs"].get("id") or state.get("_effective_inputs", {}).get("id")
        with lock:
            log.append((record, task["name"]))
        await asyncio.sleep(random.uniform(0, 0.03))
        if (record, task["name"]) in failing:
            raise RuntimeError("scripted failure")
        return {o["name"]: f"{task['name']}:{record}" for o in task.get("outputs") or []}

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
    return {"log": log, "failing": failing}


def three_nodes():
    return make_graph(
        [make_task("a", inputs=["id"], outputs=["x"]),
         make_task("b", inputs=["x"], outputs=["y"]),
         make_task("c", inputs=["y"], outputs=["z"])],
        [("a", "b"), ("b", "c")])


def run(batches, records, **kw):
    kw.setdefault("mode", "node")
    batch_id = batches.start_batch(three_nodes(), records, {"type": "upload"}, **kw)
    assert batches.wait_for(batch_id, timeout=20)
    return batches.get_batch(batch_id)


def positions(log, node):
    return [i for i, (_, name) in enumerate(log) if name == node]


def test_a_chunk_moves_through_the_workflow_one_node_at_a_time(batches, calls):
    batch = run(batches, [{"id": f"r{i}"} for i in range(6)], workers=3)

    assert batch["status"] == "succeeded"
    assert batch["mode"] == "node"
    log = calls["log"]
    assert max(positions(log, "a")) < min(positions(log, "b"))
    assert max(positions(log, "b")) < min(positions(log, "c"))
    assert len(log) == 18


def test_chunks_run_one_after_another(batches, calls, monkeypatch):
    """A DataLoader batch is a chunk: the next starts only when this one is done."""
    records = [{"id": f"r{i}"} for i in range(6)]
    # The chunk size is the DataLoader's own read batch size.
    source = {"type": "canvas", "config": {"type": "dataloader", "read_batch_size": 3}}
    batch_id = batches.start_batch(three_nodes(), records, source, workers=3, mode="node")
    assert batches.wait_for(batch_id, timeout=20)

    log = calls["log"]
    first = {f"r{i}" for i in range(3)}
    last_of_first = max(i for i, (record, _) in enumerate(log) if record in first)
    first_of_second = min(i for i, (record, _) in enumerate(log) if record not in first)
    assert last_of_first < first_of_second


def test_a_failed_record_does_not_hold_the_others_back(batches, calls):
    calls["failing"].add(("r1", "b"))

    batch = run(batches, [{"id": f"r{i}"} for i in range(4)], workers=2)

    statuses = {item["inputs"]["id"]: item["status"] for item in batch["items"]}
    assert statuses == {"r0": "success", "r1": "failed", "r2": "success", "r3": "success"}
    # r1 never reached c; everyone else did, after every b.
    assert ("r1", "c") not in calls["log"]
    log = calls["log"]
    assert max(positions(log, "b")) < min(positions(log, "c"))


def test_model_calls_in_flight_are_bounded_by_workers(batches, calls, monkeypatch):
    from backend.api import runner
    in_flight, peak = [0], [0]
    lock = threading.Lock()
    original = runner.execute_llm_node

    async def counting(agent, task, inputs, state):
        with lock:
            in_flight[0] += 1
            peak[0] = max(peak[0], in_flight[0])
        try:
            return await original(agent, task, inputs, state)
        finally:
            with lock:
                in_flight[0] -= 1

    monkeypatch.setattr(runner, "execute_llm_node", counting)

    run(batches, [{"id": f"r{i}"} for i in range(8)], workers=2)

    assert 1 <= peak[0] <= 2


def test_record_mode_still_walks_each_record_through_the_workflow(batches, calls):
    batch = run(batches, [{"id": f"r{i}"} for i in range(4)], workers=1, mode="record")

    assert batch["mode"] == "record"
    # One worker, one record at a time: a, b, c for r0 before anything of r1.
    assert [name for _, name in calls["log"][:3]] == ["a", "b", "c"]
    assert {record for record, _ in calls["log"][:3]} == {"r0"}


def test_the_stage_says_which_node_the_chunk_is_on(batches, calls, monkeypatch):
    from backend.api import runner
    seen = []
    original = runner.execute_llm_node

    async def watching(agent, task, inputs, state):
        batch_id = state.get("batch_id")
        stage = (batches.get_batch(batch_id) or {}).get("stage") if batch_id else None
        if stage:
            seen.append((stage["node"], stage["index"], stage["of"], stage["total"]))
        return await original(agent, task, inputs, state)

    monkeypatch.setattr(runner, "execute_llm_node", watching)

    batch = run(batches, [{"id": f"r{i}"} for i in range(4)], workers=4)

    assert {(node, index) for node, index, _, _ in seen} <= {("a", 1), ("b", 2), ("c", 3)}
    assert {of for _, _, of, _ in seen} == {3}
    assert batch["stage"] is None                     # settled: no stage left


def test_an_old_batch_without_a_mode_reads_as_record_mode(batches, tmp_path):
    import json
    (tmp_path / "batches").mkdir(parents=True, exist_ok=True)
    (tmp_path / "batches" / "old.json").write_text(json.dumps({
        "batch_id": "old", "graph_id": "g", "status": "succeeded", "items": [],
        "created_at": "2026-09-01T00:00:00+00:00"}))

    listed = [b for b in batches.list_batches() if b["batch_id"] == "old"]
    assert listed and listed[0]["mode"] == "record"


def test_the_gate_lets_records_into_a_node_in_batch_order():
    """Calls start in record order: the second record cannot begin a node
    before the first has begun it, even if its thread is ready first."""
    from backend.features.execution.node_gate import NodeGate
    gate = NodeGate(["a"], workers=5)
    for run_id in ("r0", "r1", "r2"):
        gate.expect(run_id)
    order = []
    started = threading.Event()

    def enter(run_id):
        gate.wait_turn(run_id, 0, model_call=True)
        order.append(run_id)
        started.set()

    late_first = threading.Thread(target=enter, args=("r2",))
    late_first.start()
    time.sleep(0.3)
    assert order == []                    # r2 waits for r0 and r1 to begin
    for run_id in ("r1", "r0"):
        threading.Thread(target=enter, args=(run_id,)).start()
    late_first.join(5)
    time.sleep(0.2)
    assert order[0] == "r0" and set(order) == {"r0", "r1", "r2"}


def test_a_record_that_leaves_never_holds_the_chunk():
    from backend.features.execution.node_gate import NodeGate
    gate = NodeGate(["a", "b"], workers=5)
    for run_id in ("r0", "r1"):
        gate.expect(run_id)
    gate.leave("r0")                      # stopped before it ever started

    assert gate.wait_turn("r1", 0) is False   # no model slot asked for
    gate.done("r1", 0)
    gate.wait_turn("r1", 1)                   # returns: nobody left behind


def with_memory():
    """The combination that used to serialise a whole batch: a node that
    reads its own memory, over DataLoader-grouped records."""
    graph = three_nodes()
    graph["tasks"][1].update(use_long_term_memory=True,
                             memory={"kind": "table", "match": "id", "read_from": ["b"],
                                     "retrieve": 3, "when": "success"})
    return graph


def grouped(n):
    return [{"id": f"r{i}", "_dataloader": {"group": f"g{i}"}} for i in range(n)]


def test_memory_no_longer_serialises_the_records_of_a_chunk(batches, calls):
    batch_id = batches.start_batch(with_memory(), grouped(4), {"type": "upload"},
                                   workers=4, mode="node")
    assert batches.wait_for(batch_id, timeout=20)
    batch = batches.get_batch(batch_id)

    assert [item["status"] for item in batch["items"]] == ["success"] * 4
    log = calls["log"]
    assert max(positions(log, "a")) < min(positions(log, "b"))
    assert max(positions(log, "b")) < min(positions(log, "c"))
    assert batch["sequencing"]["mode"] == "chunk"


def test_one_failure_blocks_its_own_subject_not_the_batch(batches, calls):
    calls["failing"].add(("r1", "a"))

    batch_id = batches.start_batch(with_memory(), grouped(4), {"type": "upload"},
                                   workers=4, mode="node")
    assert batches.wait_for(batch_id, timeout=20)
    statuses = {item["inputs"]["id"]: item["status"]
                for item in batches.get_batch(batch_id)["items"]}

    assert statuses == {"r0": "success", "r1": "failed", "r2": "success", "r3": "success"}
