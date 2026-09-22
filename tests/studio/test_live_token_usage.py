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


class _Model:
    """A model whose response hook the Studio observes, as a real adapter's."""

    def _update_cost(self, response):
        return None


def _response(prompt, completion):
    return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion))


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
    """The real run engine; each LLM node reports usage, then waits to be let go."""
    from evoagentx.models import LiteLLMConfig
    from backend.api import runner, tools_registry
    gates: dict[str, threading.Event] = {}
    reported: list[str] = []

    async def fake_llm(agent, task, inputs, state):
        agent.llm._update_cost(_response(10, 5))
        reported.append(task["name"])
        gate = gates.setdefault(f"{state['run_id']}:{task['name']}", threading.Event())
        await asyncio.to_thread(gate.wait, 10)
        return {o["name"]: "ok" for o in task.get("outputs") or []}

    class StubLLM:
        config = LiteLLMConfig(model="deepseek/deepseek-chat", deepseek_key="test-only")

    monkeypatch.setattr(runner, "execute_llm_node", fake_llm)
    monkeypatch.setattr(runner, "_make_llm", lambda: StubLLM())
    monkeypatch.setattr(runner, "_agent_for_node", lambda manager, node: SimpleNamespace(llm=_Model()))
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

    yield SimpleNamespace(release=release, release_all=release_all, reported=reported, gates=gates)
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
    assert live == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
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
    from langchain_core.messages import AIMessage
    from backend.features.execution import provider_batch

    def submit(messages, state, **kwargs):
        future = Future()
        future.set_result(AIMessage(content="hi", usage_metadata={
            "input_tokens": 7, "output_tokens": 3, "total_tokens": 10}))
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


def test_a_model_moved_to_another_run_counts_only_there():
    from backend.features.execution.token_usage import attach_model
    model, first, second = _Model(), {}, {}
    attach_model(model, first)
    attach_model(model, second)
    model._update_cost(_response(2, 1))
    assert first == {}
    assert second["token_usage"]["total_tokens"] == 3


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
