"""Token usage is visible while work is in progress, not only when it ends.

Each model call's provider-reported usage reaches the run, its node, the
batch and the live endpoints as the call returns; a finished item's usage is
counted once; nothing reported is never shown as zero.
"""

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest


def _result(prompt, completion):
    """What the `llm` package returns for one call."""
    from llm import LLMResult, LLMUsage
    return LLMResult(content="ok", provider="test", model="test-model",
                     usage=LLMUsage(input_tokens=prompt, output_tokens=completion,
                                    total_tokens=prompt + completion))


def _wait(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition not reached")


@pytest.fixture
def engine(studio_data, monkeypatch):
    """The real run engine and the real bridge model; only the package's call
    is replaced, so each node's usage travels the way a real call's does."""
    from backend.api import runner, tools_registry
    from backend.features import model_bridge
    gates: dict[str, threading.Event] = {}
    reported: list[str] = []
    models: list = []

    monkeypatch.setattr(model_bridge, "chat_result",
                        lambda provider, messages, **options: _result(10, 5))

    async def fake_llm(agent, task, inputs, state):
        # One model call, through the bridge and the package, as a node makes it.
        agent.llm.single_generate([{"role": "user", "content": task["name"]}])
        reported.append(task["name"])
        gate = gates.setdefault(f"{state['run_id']}:{task['name']}", threading.Event())
        await asyncio.to_thread(gate.wait, 10)
        return {o["name"]: "ok" for o in task.get("outputs") or []}

    real_make_llm = runner._make_llm

    def make_llm(**kwargs):
        models.append(real_make_llm(**kwargs))
        return models[-1]

    monkeypatch.setattr(runner, "execute_llm_node", fake_llm)
    monkeypatch.setattr(runner, "_make_llm", make_llm)
    monkeypatch.setattr(runner, "_agent_for_node",
                        lambda manager, node: SimpleNamespace(llm=models[-1]))
    monkeypatch.setattr(runner, "_prepare_ltm", lambda doc, ordered, inputs, state: ({}, ordered))
    monkeypatch.setattr(runner, "_attach_ltm", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_save_ltm", lambda *a, **k: None)
    monkeypatch.setattr(tools_registry, "validate_tool_names", lambda names: None)
    monkeypatch.setattr(tools_registry, "resolve_tools", lambda names, **kw: None)
    runner._runs.clear()

    def release(run_id, node):
        gates.setdefault(f"{run_id}:{node}", threading.Event()).set()

    def release_all():
        for gate in gates.values():
            gate.set()

    yield SimpleNamespace(release=release, release_all=release_all, reported=reported,
                          gates=gates, models=models)
    release_all()


def _two_nodes():
    from conftest import make_graph, make_task
    return make_graph([make_task("a", inputs=["text"], outputs=["x"]),
                       make_task("b", inputs=["x"], outputs=["y"])],
                      edges=[("a", "b")], id="live-usage")


def test_a_running_run_reports_usage_per_call_and_per_node(engine):
    from backend.api import runner
    run_id = runner.start_run(_two_nodes(), {"text": "t"})

    live = _wait(lambda: (runner.get_run(run_id) or {}).get("token_usage"))
    run = runner.get_run(run_id)
    assert run["status"] == "running"
    # Cache and reasoning counts ride along with what the package reports
    # (zero here: the stub's LLMUsage leaves them at their defaults).
    assert live == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                    "cache_read_tokens": 0, "reasoning_tokens": 0,
                    "reported_calls": 1, "source": "provider"}
    nodes = {n["name"]: n for n in run["nodes"]}
    assert nodes["a"]["token_usage"]["total_tokens"] == 15
    assert "token_usage" not in nodes["b"]      # nothing reported: not a zero

    engine.release(run_id, "a")
    _wait(lambda: (runner.get_run(run_id) or {}).get("token_usage", {}).get("reported_calls") == 2)
    run = runner.get_run(run_id)
    assert run["status"] == "running"
    assert run["token_usage"]["total_tokens"] == 30
    assert {n["name"]: n.get("token_usage", {}).get("total_tokens") for n in run["nodes"]} == {"a": 15, "b": 15}

    engine.release(run_id, "b")
    done = _wait(lambda: (runner.get_run(run_id) or {}).get("status") == "success" and runner.get_run(run_id))
    assert done["token_usage"]["total_tokens"] == 30


def test_a_run_without_reported_usage_stays_unavailable(engine, monkeypatch):
    from backend.api import runner

    async def silent(agent, task, inputs, state):
        return {o["name"]: "ok" for o in task.get("outputs") or []}

    monkeypatch.setattr(runner, "execute_llm_node", silent)
    run_id = runner.start_run(_two_nodes(), {"text": "t"}, background=False)
    run = runner.get_run(run_id)
    assert run["status"] == "success"
    assert run.get("token_usage") is None
    assert all("token_usage" not in n for n in run["nodes"])


@pytest.fixture
def batches(tmp_path, monkeypatch):
    from backend.api import batch as batch_module
    monkeypatch.setattr(batch_module, "BATCHES_DIR", tmp_path / "batches")
    monkeypatch.setattr(batch_module, "_pause", lambda seconds, batch_id=None: None)
    batch_module._batches.clear()
    batch_module._threads.clear()
    yield batch_module


def test_a_batch_counts_in_flight_runs_and_settles_them_once(engine, batches):
    from conftest import make_graph, make_task
    graph = make_graph([make_task("a", inputs=["text"], outputs=["x"])], id="live-batch")
    batch_id = batches.start_batch(graph, [{"text": "1"}, {"text": "2"}], {"type": "manual"}, workers=2)

    _wait(lambda: len(engine.reported) == 2)
    live = batches.get_batch(batch_id)
    assert live["status"] == "running"
    assert live["token_usage"]["total_tokens"] == 30
    assert live["token_usage"]["reported_calls"] == 2
    assert all(i["token_usage"]["total_tokens"] == 15 for i in live["items"])
    assert live["node_progress"]["a"]["token_usage"]["total_tokens"] == 30
    digest = next(b for b in batches.list_batches("live-batch") if b["batch_id"] == batch_id)
    assert digest["token_usage"]["total_tokens"] == 30
    # The live view is computed, never written into the batch itself.
    assert not batches._batches[batch_id].get("token_usage")

    first = live["items"][0]["run_id"]
    engine.release(first, "a")
    _wait(lambda: batches.get_batch(batch_id)["items"][0]["status"] == "success")
    middle = batches.get_batch(batch_id)
    assert middle["token_usage"]["total_tokens"] == 30          # one settled + one in flight
    assert batches._batches[batch_id]["token_usage"]["total_tokens"] == 15

    engine.release_all()
    assert batches.wait_for(batch_id, timeout=20)
    final = batches.get_batch(batch_id)
    assert final["status"] == "succeeded"
    assert final["token_usage"]["total_tokens"] == 30
    assert final["token_usage"]["reported_calls"] == 2


def test_a_session_retry_does_not_count_the_previous_attempt_as_live(batches, monkeypatch):
    from backend.api import runner
    item = {"status": "running", "run_id": "same", "attempt_started_at": "2026-01-02T00:00:00+00:00"}
    old = {"created_at": "2026-01-01T00:00:00+00:00", "token_usage": {"total_tokens": 9, "reported_calls": 1}}
    monkeypatch.setattr(runner, "get_run", lambda run_id: old)
    assert batches._in_flight_usage(item) is None
    old["created_at"] = "2026-01-03T00:00:00+00:00"
    assert batches._in_flight_usage(item)["total_tokens"] == 9


def test_provider_batch_results_report_usage_as_they_arrive(monkeypatch):
    from concurrent.futures import Future
    from llm import LLMResult, LLMUsage
    from backend.features.execution import provider_batch

    def submit(messages, state, **kwargs):
        future = Future()
        future.set_result(LLMResult(content="hi", usage=LLMUsage(
            input_tokens=7, output_tokens=3, total_tokens=10)))
        return future

    monkeypatch.setattr(provider_batch, "validate", lambda graph, value: None)
    monkeypatch.setattr(provider_batch, "submit", submit)
    node = {"name": "a"}
    state = {"_usage_node": node}
    model = provider_batch.attach_workflow_model(SimpleNamespace(), state)
    assert model.single_generate([{"role": "user", "content": "x"}]) == "hi"
    assert state["token_usage"]["total_tokens"] == 10
    assert node["token_usage"]["total_tokens"] == 10
    monkeypatch.setattr(provider_batch, "submit", lambda *a, **k: _done("plain text"))
    model.single_generate([{"role": "user", "content": "x"}])
    assert state["token_usage"]["reported_calls"] == 1     # a bare string reports nothing


def _done(value):
    from concurrent.futures import Future
    future = Future()
    future.set_result(value)
    return future


def test_each_run_counts_only_its_own_calls_and_nothing_after_it_settles(monkeypatch):
    """The hook is per run and released with it: a call made afterwards is
    counted nowhere rather than into the run that has already been reported."""
    from backend.features import model_bridge
    from backend.features.execution import token_usage
    first, second = {}, {}
    first_key = token_usage.usage_key(first)
    second_key = token_usage.usage_key(second)
    assert first_key != second_key

    model_bridge._report(second_key, _result(2, 1))
    assert first == {"_usage_key": first_key}
    assert second["token_usage"]["total_tokens"] == 3

    token_usage.release_usage(second)
    model_bridge._report(second_key, _result(5, 5))
    assert second["token_usage"]["total_tokens"] == 3
    assert "_usage_key" not in second
    token_usage.release_usage(first)


def test_an_evolve_task_shows_its_in_flight_run(monkeypatch):
    evolve_api = pytest.importorskip("backend.features.evaluation.evolve_api")
    from backend.api import runner
    monkeypatch.setattr(runner, "get_run", lambda run_id: {"token_usage": {
        "input_tokens": 4, "output_tokens": 1, "total_tokens": 5, "reported_calls": 1}})
    state = {"task_id": "t", "current_run": "r", "token_usage": {
        "input_tokens": 1, "output_tokens": 1, "total_tokens": 2, "reported_calls": 1}}
    assert evolve_api._live(state)["token_usage"]["total_tokens"] == 7
    assert state["token_usage"]["total_tokens"] == 2
    assert evolve_api._live({"task_id": "t"}) == {"task_id": "t"}
