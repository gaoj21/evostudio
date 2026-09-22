"""Stop has to mean stop, everywhere something can be started.

One audit, one file: each class is a layer that can start work — a run, a
batch, the coalescer in front of the provider API, a streaming Dataset's
reader process, a watcher — and each asserts the same three things about it.
After Stop: no new model call, no new subprocess, and the thing settles in
seconds rather than whenever its own timers happen to expire.

Every model call here is a stand-in (`runner.execute_llm_node`,
`runner._make_llm`, `llm.batch`); slow work is a sleep or a real sleeping
subprocess. Nothing reaches a provider.
"""

import asyncio
import json
import os
import signal
import threading
import time

import pytest


def _wait_until(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def batch_store(tmp_path, monkeypatch):
    from backend.api import batch as batch_module
    monkeypatch.setattr(batch_module, "BATCHES_DIR", tmp_path / "batches")
    batch_module._batches.clear()
    batch_module._threads.clear()
    batch_module._stops.clear()
    yield batch_module
    # A batch persists itself from its worker thread after setting its final
    # status; ending the test before that write would land it in the real
    # studio-data once monkeypatch has restored the path.
    for batch_id in list(batch_module._threads):
        assert batch_module.wait_for(batch_id, timeout=20), \
            f"batch {batch_id} did not wind down; its state would leak"


@pytest.fixture
def runs(tmp_path, monkeypatch):
    from backend.api import runner
    monkeypatch.setattr(runner, "RUNS_DIR", tmp_path / "runs")
    runner._runs.clear()
    runner._pending_cancels.clear()
    return runner


class TestARunStoppedBeforeItStarts:
    """The window between publishing a run id and binding it.

    A batch item and an Evolve candidate both choose their run id, put it on
    the item so live progress is visible, and only then call `start_run`. A
    Stop landing in that window found no such run, said `{"cancelled": False,
    "reason": "no such run"}` — and the record went on to make every model
    call it had, while the batch reported it as interrupted.
    """

    def test_an_expected_id_holds_the_stop_until_the_run_starts(self, runs):
        assert runs.cancel_run("not-started-yet", expected=True) == \
            {"cancelled": True, "before_start": True}

    def test_an_unexpected_id_is_still_reported_as_unknown(self, runs):
        assert runs.cancel_run("nope") == {"cancelled": False, "reason": "no such run"}
        assert not runs._pending_cancels

    def test_the_held_stop_lands_before_the_first_model_call(self, runs, monkeypatch):
        from conftest import make_graph, make_task
        calls = []

        async def recording_llm(agent, task, inputs, state):
            calls.append(task["name"])
            return {o["name"]: "answer" for o in task.get("outputs") or []}

        monkeypatch.setattr(runs, "execute_llm_node", recording_llm)
        monkeypatch.setattr(runs, "_make_llm", lambda: _StubLLM())
        _neutralise_memory(runs, monkeypatch)
        graph = make_graph([make_task("a", inputs=["topic"], outputs=["x"])])
        graph["id"] = "stop-before-start"

        runs.cancel_run("held", expected=True)
        runs.start_run(graph, {"topic": "t"}, background=False, run_id="held")

        run = runs.get_run("held")
        assert run["status"] == "cancelled"
        assert calls == []                        # nothing was asked of a model
        assert not runs._pending_cancels          # the hold was consumed

    def test_a_batch_item_in_that_window_never_reaches_the_model(
            self, runs, batch_store, monkeypatch):
        from conftest import make_graph, make_task
        calls = []
        published = threading.Event()
        released = threading.Event()

        async def recording_llm(agent, task, inputs, state):
            calls.append(inputs)
            return {o["name"]: "answer" for o in task.get("outputs") or []}

        monkeypatch.setattr(runs, "execute_llm_node", recording_llm)
        monkeypatch.setattr(runs, "_make_llm", lambda: _StubLLM())
        _neutralise_memory(runs, monkeypatch)

        real_start_run = runs.start_run

        def start_run_late(*args, **kwargs):
            # Hold the item exactly where the race was: its id is on the batch,
            # the engine has not seen it yet.
            published.set()
            assert released.wait(10)
            return real_start_run(*args, **kwargs)

        monkeypatch.setattr(batch_store.runner, "start_run", start_run_late)
        graph = make_graph([make_task("a", inputs=["topic"], outputs=["x"])])
        graph["id"] = "stop-the-gap"
        batch_id = batch_store.start_batch(graph, [{"topic": str(i)} for i in range(4)],
                                           {"type": "manual"}, workers=1)

        assert published.wait(10)
        assert batch_store.cancel_batch(batch_id)["interrupted"] == 1
        released.set()

        assert batch_store.wait_for(batch_id, timeout=20)
        assert calls == []                        # the promise the count made
        statuses = [i["status"] for i in batch_store.get_batch(batch_id)["items"]]
        assert statuses == ["cancelled"] * 4
        assert batch_store.get_batch(batch_id)["status"] == "cancelled"


class TestARunWaitingOnTheModel:
    """The ordinary case: a node is inside a model call, or inside the wait
    between its retries. Both are awaits on the run's own loop, so a stop
    reaches them; nothing may be written to memory afterwards."""

    def test_a_stop_during_a_retry_wait_lands_at_once(self, runs, monkeypatch):
        from conftest import make_graph, make_task
        waiting = threading.Event()

        async def retrying_llm(agent, task, inputs, state):
            # What the model layer does while a provider is unreachable: ask,
            # wait, ask again — the wait an awaitable, so a stop lands in it.
            for _attempt in range(10):
                waiting.set()
                await asyncio.sleep(30)
            return {}

        monkeypatch.setattr(runs, "execute_llm_node", retrying_llm)
        monkeypatch.setattr(runs, "_make_llm", lambda: _StubLLM())
        _neutralise_memory(runs, monkeypatch)
        graph = make_graph([make_task("a", inputs=["topic"], outputs=["x"])])
        graph["id"] = "stop-mid-retry"

        run_id = runs.start_run(graph, {"topic": "t"}, run_id="retrying")
        assert waiting.wait(20), "the node never reached its retry wait"
        started = time.time()
        assert runs.cancel_run(run_id)["cancelled"] is True
        assert _wait_until(lambda: (runs.get_run(run_id) or {}).get("status") == "cancelled", 10)
        assert time.time() - started < 5     # not the 30s the wait asked for

    def test_nothing_is_written_to_memory_after_a_stop(self, runs, monkeypatch):
        from conftest import make_graph, make_task
        from backend.api import tools_registry
        writes = []
        running = threading.Event()

        async def blocking_llm(agent, task, inputs, state):
            running.set()
            await asyncio.sleep(30)
            return {}

        monkeypatch.setattr(runs, "execute_llm_node", blocking_llm)
        monkeypatch.setattr(runs, "_make_llm", lambda: _StubLLM())
        monkeypatch.setattr(runs, "_prepare_ltm",
                            lambda doc, ordered, inputs, state: ({}, ordered))
        monkeypatch.setattr(runs, "_attach_ltm", lambda *a, **k: None)
        monkeypatch.setattr(runs, "_save_ltm",
                            lambda *a, **k: writes.append(k.get("succeeded", True)))
        monkeypatch.setattr(tools_registry, "validate_tool_names", lambda names: None)
        monkeypatch.setattr(tools_registry, "resolve_tools", lambda names, **kw: None)
        graph = make_graph([make_task("a", inputs=["topic"], outputs=["x"],
                                      use_long_term_memory=True)])
        graph["id"] = "stop-before-memory"

        run_id = runs.start_run(graph, {"topic": "t"}, run_id="memory-stop")
        assert running.wait(20)
        runs.cancel_run(run_id)
        assert _wait_until(lambda: (runs.get_run(run_id) or {}).get("status") == "cancelled", 10)
        time.sleep(.5)
        # A stopped run remembers nothing — not even as a failure, which is
        # what the `succeeded=False` write would have recorded it as.
        assert writes == []


class TestARunInsideATool:
    """A tool node is someone else's process and a blocking wait on it.

    The run's loop is not inside an `await` while a tool call is in flight, so
    cancelling the task changes nothing until the call returns — and the
    worker holding the tool (and whatever it started) is not waiting for
    anybody. Stop has to reach the process and settle the run regardless.
    """

    def graph(self, tool="sleeper"):
        return {"id": "tool-stop", "name": "Tool", "goal": "stop a tool",
                "flow_version": 2, "output_dir": "runs", "edges": [],
                "tasks": [{"name": "work", "kind": "tool", "tool": tool,
                           "inputs": [{"name": "text", "type": "str", "required": True}],
                           "outputs": [{"name": "answer", "type": "str", "required": True}]}]}

    def test_the_run_settles_while_the_tool_is_still_blocking(self, runs, monkeypatch):
        from backend.api import tools_registry
        calling, release = threading.Event(), threading.Event()

        def blocking_tool(name, args, **kwargs):
            calling.set()
            release.wait(60)
            return {"answer": "too late"}

        monkeypatch.setattr(tools_registry, "find_tool", lambda name: ("custom", None))
        monkeypatch.setattr(tools_registry, "validate_tool_names", lambda names: None)
        monkeypatch.setattr(tools_registry, "call_tool", blocking_tool)

        run_id = runs.start_run(self.graph(), {"text": "x"}, run_id="tool-run")
        try:
            assert calling.wait(20), "the tool was never called"
            started = time.time()
            assert runs.cancel_run(run_id)["cancelled"] is True
            assert _wait_until(
                lambda: (runs.get_run(run_id) or {}).get("status") == "cancelled", 10), \
                "the run stayed 'running' behind an uninterruptible tool call"
            assert time.time() - started < 6
            # Said plainly: this one could not be taken back.
            assert "background" in (runs.get_run(run_id).get("error") or "")
        finally:
            release.set()

    def test_the_tool_worker_process_is_killed(self, runs, studio_data, tmp_path):
        """A real custom tool, in the worker a real run would start."""
        from backend.api import custom_tools
        pid_file = tmp_path / "tool.pid"
        custom_tools.save_custom_tool(custom_tools.validate_spec({"code": (
            "import os, time\n"
            "\n"
            "\n"
            "def sleeper(text: str) -> dict:\n"
            '    """Sleep, as a tool waiting on something slow does.\n'
            "\n"
            "    Args:\n"
            "        text: ignored\n"
            '    """\n'
            f"    open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
            "    time.sleep(300)\n"
            "    return {'answer': 'too late'}\n")}, []))

        run_id = runs.start_run(self.graph(), {"text": "x"}, run_id="tool-worker-run")
        pid = None
        try:
            assert _wait_until(lambda: pid_file.is_file(), 60), "the tool never ran"
            pid = int(pid_file.read_text())
            assert _alive(pid)

            runs.cancel_run(run_id)
            assert _wait_until(lambda: not _alive(pid), 10), \
                "the tool's worker process outlived the stop"
            assert _wait_until(
                lambda: (runs.get_run(run_id) or {}).get("status") == "cancelled", 10)
        finally:
            if pid is not None:
                _kill(pid)


class TestAnEvaluatorTheRunStarted:
    """An evaluator is the run's own work: Python in a worker process, started
    by the node that just finished. A stop has to take it too, rather than
    leave the run 'running' for the evaluator's whole time limit."""

    def graph(self):
        return {"id": "evaluator-stop", "name": "Evaluator", "goal": "stop an evaluator",
                "flow_version": 2, "output_dir": "runs",
                "tasks": [{"name": "work", "kind": "tool", "tool": "echo",
                           "inputs": [{"name": "text", "type": "str", "required": True}],
                           "outputs": [{"name": "answer", "type": "str", "required": True}]},
                          {"name": "quality", "kind": "evaluator", "outputs": [],
                           "inputs": [{"name": "prediction", "type": "any", "required": True}],
                           "evaluator": {"type": "python", "timing": "node", "metric": "score",
                                         "code": None}}],
                "edges": [{"source": "work", "target": "quality",
                           "mappings": [{"from": "answer", "to": "prediction"}]}]}

    def test_its_worker_dies_and_the_run_settles(self, runs, monkeypatch, tmp_path):
        from backend.api import tools_registry
        pid_file = tmp_path / "evaluator.pid"
        monkeypatch.setattr(tools_registry, "find_tool", lambda name: ("custom", None))
        monkeypatch.setattr(tools_registry, "validate_tool_names", lambda names: None)
        monkeypatch.setattr(tools_registry, "call_tool",
                            lambda name, args, **kw: {"answer": args["text"]})
        graph = self.graph()
        graph["tasks"][1]["evaluator"]["code"] = (
            "import os, time\n"
            "\n"
            "\n"
            "def evaluate(records):\n"
            f"    open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
            "    time.sleep(300)\n"
            "    return {'metrics': {'score': 1}}\n")

        run_id = runs.start_run(graph, {"text": "x"}, run_id="evaluator-run")
        pid = None
        try:
            assert _wait_until(lambda: pid_file.is_file(), 60), "the evaluator never ran"
            pid = int(pid_file.read_text())
            started = time.time()
            runs.cancel_run(run_id)
            assert _wait_until(lambda: not _alive(pid), 10), \
                "the evaluator's worker outlived the stop"
            assert _wait_until(
                lambda: (runs.get_run(run_id) or {}).get("status") == "cancelled", 10)
            assert time.time() - started < 8
        finally:
            if pid is not None:
                _kill(pid)


class TestTheRetryPause:
    """`RETRY_PAUSE_SECONDS` is a minute, and it was a plain `time.sleep`.

    Stop during it left the batch reading `cancelling` for the whole minute —
    the Stop button disabled and labelled "Stopping…" while nothing at all was
    running.
    """

    @pytest.fixture
    def unreachable_model(self, batch_store, monkeypatch):
        from backend.api import runner
        paused = threading.Event()

        def fake_start_run(graph, inputs, background=True, run_id=None, **_kw):
            runner._runs[run_id] = {
                "run_id": run_id, "status": "failed", "nodes": [], "review_status": None,
                "error": "no model", "node_error": {"retryable": True}}
            return run_id

        real_pause = batch_store._pause

        def watched_pause(seconds, batch_id=None):
            paused.set()
            real_pause(seconds, batch_id)

        monkeypatch.setattr(batch_store.runner, "start_run", fake_start_run)
        monkeypatch.setattr(batch_store.runner, "get_run", lambda rid: runner._runs.get(rid))
        monkeypatch.setattr(batch_store, "_pause", watched_pause)
        monkeypatch.setattr(batch_store, "RETRY_PAUSE_SECONDS", 30.0)
        return paused

    def test_a_stop_during_it_settles_the_batch_in_seconds(
            self, batch_store, unreachable_model):
        batch_id = batch_store.start_batch(
            {"id": "g"}, [{"n": i} for i in range(2)], {"type": "manual"}, workers=1)
        assert unreachable_model.wait(10), "the batch never reached its retry pause"

        started = time.time()
        assert batch_store.cancel_batch(batch_id)["cancelled"] is True
        assert _wait_until(
            lambda: batch_store.get_batch(batch_id)["status"] == "cancelled", 10)
        # Seconds, not the 30 the pause wanted (60 as shipped).
        assert time.time() - started < 5
        # No "trying again in 60s" left on a batch that is not going to.
        assert "retry_note" not in batch_store.get_batch(batch_id)

    def test_an_uncancelled_batch_still_waits_out_its_pause(self, batch_store, monkeypatch):
        slept = []
        monkeypatch.setattr(batch_store.time, "sleep", lambda s: slept.append(s))
        batch_store._pause(7.5, "no-such-batch")
        assert slept == [7.5]


class TestABatchThatIsStillEvaluating:
    """A batch's own evaluators run when its records are done — including
    when they are done because the user stopped them.

    Scoring what did run is worth having. Waiting for it before admitting the
    batch has stopped is not: the evaluator has two minutes of its own, and
    the Stop button says "Stopping…" for all of them.
    """

    @pytest.fixture
    def graph_with_slow_evaluator(self, tmp_path):
        code = ("import time\n"
                "\n"
                "\n"
                "def evaluate(records):\n"
                f"    open({str(tmp_path / 'evaluating')!r}, 'w').write('yes')\n"
                "    time.sleep(6)\n"
                "    return {'metrics': {'score': len(records)}}\n")
        return {"id": "g", "tasks": [
            {"name": "check", "kind": "evaluator", "enabled": True,
             "evaluator": {"type": "python", "timing": "batch", "metric": "score",
                           "code": code}}]}

    def test_the_batch_says_it_stopped_without_waiting_for_the_report(
            self, batch_store, graph_with_slow_evaluator, monkeypatch):
        from backend.api import runner
        running = threading.Event()

        def fake_start_run(graph, inputs, background=True, run_id=None, **_kw):
            running.set()
            runner._runs[run_id] = {"run_id": run_id, "status": "success", "result": inputs,
                                    "nodes": [], "error": None, "review_status": None}
            return run_id

        monkeypatch.setattr(batch_store.runner, "start_run", fake_start_run)
        monkeypatch.setattr(batch_store.runner, "get_run", lambda rid: runner._runs.get(rid))
        batch_id = batch_store.start_batch(graph_with_slow_evaluator,
                                           [{"n": i} for i in range(2)],
                                           {"type": "manual"}, workers=1)
        assert running.wait(10)
        started = time.time()
        batch_store.cancel_batch(batch_id)
        assert _wait_until(
            lambda: batch_store.get_batch(batch_id)["status"] == "cancelled", 5), \
            "the batch read 'cancelling' until its evaluator was finished"
        assert time.time() - started < 5

        # And the report over what did run still arrives, on the same batch.
        assert batch_store.wait_for(batch_id, timeout=30)
        settled = batch_store.get_batch(batch_id)
        assert settled["status"] == "cancelled"
        assert settled["evaluations"]["check"]["status"] == "success"

    def test_no_record_is_run_for_the_evaluators(
            self, batch_store, graph_with_slow_evaluator, monkeypatch):
        """Evaluating is reading what happened; it never starts a workflow."""
        from backend.api import runner
        runs_started = []
        running = threading.Event()

        def fake_start_run(graph, inputs, background=True, run_id=None, **_kw):
            runs_started.append(inputs)
            running.set()
            runner._runs[run_id] = {"run_id": run_id, "status": "success", "result": inputs,
                                    "nodes": [], "error": None, "review_status": None}
            return run_id

        monkeypatch.setattr(batch_store.runner, "start_run", fake_start_run)
        monkeypatch.setattr(batch_store.runner, "get_run", lambda rid: runner._runs.get(rid))
        batch_id = batch_store.start_batch(graph_with_slow_evaluator,
                                           [{"n": i} for i in range(6)],
                                           {"type": "manual"}, workers=1)
        assert running.wait(10)
        batch_store.cancel_batch(batch_id)
        assert batch_store.wait_for(batch_id, timeout=30)
        after_stop = len(runs_started)
        time.sleep(1)
        assert len(runs_started) == after_stop < 6


class TestABatchInsideSomethingUninterruptible:
    """One record's run may be in a call with no interruption point. The batch
    must not report itself as "Stopping…" until that call comes back."""

    def test_the_batch_settles_although_a_record_is_still_in_flight(
            self, batch_store, monkeypatch):
        from backend.api import runner
        blocking, release = threading.Event(), threading.Event()

        def fake_start_run(graph, inputs, background=True, run_id=None, **_kw):
            blocking.set()
            assert release.wait(60)
            runner._runs[run_id] = {"run_id": run_id, "status": "success", "result": inputs,
                                    "nodes": [], "error": None, "review_status": None}
            return run_id

        monkeypatch.setattr(batch_store.runner, "start_run", fake_start_run)
        monkeypatch.setattr(batch_store.runner, "get_run", lambda rid: runner._runs.get(rid))
        batch_id = batch_store.start_batch({"id": "g"}, [{"n": i} for i in range(4)],
                                           {"type": "manual"}, workers=1)
        try:
            assert blocking.wait(10)
            started = time.time()
            batch_store.cancel_batch(batch_id)
            assert _wait_until(
                lambda: batch_store.get_batch(batch_id)["status"] == "cancelled", 10), \
                "the batch stayed 'cancelling' behind one uninterruptible record"
            assert time.time() - started < 8
            settled = batch_store.get_batch(batch_id)
            # Nothing left mid-air: every record has a verdict, and the one
            # that could not be taken back says so.
            assert not [i for i in settled["items"]
                        if i["status"] in ("pending", "running")]
            assert "background" in (settled["items"][0].get("error") or "")
            # And it cannot be resumed on top of itself while that record is
            # still in there: two workers over one batch's items.
            assert batch_store.resume_batch(batch_id, {"id": "g"}) == {
                "resumed": False,
                "reason": "the last record of this batch has not finished stopping"}
        finally:
            release.set()


class TestStoppingASteppedBatch:
    """Records that must run in order are handed to the pool as a group, and a
    group blocked by an earlier failure is walked again in later chunks. A stop
    has to end both without leaving a record without a verdict."""

    def records(self):
        # Two samples of three steps each: groups, in the batch's own terms.
        return [{"n": i, "_dataloader": {"group": f"sample-{i % 2}"}} for i in range(6)]

    def test_every_record_of_every_group_settles(self, batch_store, monkeypatch):
        from backend.api import runner
        started = []
        first = threading.Event()

        def fake_start_run(graph, inputs, background=True, run_id=None, **_kw):
            started.append(inputs["n"])
            first.set()
            time.sleep(.2)
            # The first step of each sample fails, so the rest of its group is
            # blocked — the state a stop most often lands in.
            failed = inputs["n"] < 2
            runner._runs[run_id] = {
                "run_id": run_id, "status": "failed" if failed else "success",
                "result": inputs, "nodes": [], "review_status": None,
                "error": "no" if failed else None, "node_error": None}
            return run_id

        monkeypatch.setattr(batch_store.runner, "start_run", fake_start_run)
        monkeypatch.setattr(batch_store.runner, "get_run", lambda rid: runner._runs.get(rid))
        batch_id = batch_store.start_batch({"id": "g"}, self.records(),
                                           {"type": "manual"}, workers=2)
        assert first.wait(10)
        batch_store.cancel_batch(batch_id)
        assert batch_store.wait_for(batch_id, timeout=20)

        settled = batch_store.get_batch(batch_id)
        assert settled["status"] == "cancelled"
        assert not [i for i in settled["items"] if i["status"] in ("pending", "running")]
        # The groups were not run to the end for the sake of their order.
        assert len(started) < 6


class TestTheCoalescerInFrontOfTheProvider:
    """Prompts waiting to be coalesced into one `llm.batch` call.

    The flush timer fired whatever the batch's state, so Stop was followed by
    a fresh call to the provider with every prompt queued behind it — and
    records still winding down were allowed to queue more.
    """

    @pytest.fixture
    def provider(self, monkeypatch):
        import llm
        from backend.features.execution import provider_batch
        sent = []
        monkeypatch.setattr(
            llm, "batch",
            lambda name, inputs, **kw: (sent.append(list(inputs)), ["ok"] * len(inputs))[1],
            raising=False)
        provider_batch._pending.clear()
        provider_batch._cancelled.clear()
        provider_batch._in_flight.clear()
        yield provider_batch, sent
        provider_batch._pending.clear()
        provider_batch._cancelled.clear()
        provider_batch._in_flight.clear()

    def state(self, batch_id="b1"):
        return {"batch_id": batch_id, "llm_batch_size": 8}

    def test_a_request_already_with_the_provider_is_reported_not_waited_for(
            self, monkeypatch):
        """`llm.batch` takes a list and returns results: no job handle, so a
        request in flight cannot be recalled. The record waiting on it is
        released at once and told plainly that it may still be billed —
        rather than holding the batch in `cancelling` until it returns."""
        import llm
        from backend.features.execution import provider_batch
        in_flight, release = threading.Event(), threading.Event()

        def slow_provider(name, inputs, **kwargs):
            in_flight.set()
            assert release.wait(60)
            return ["ok"] * len(inputs)

        monkeypatch.setattr(llm, "batch", slow_provider, raising=False)
        provider_batch._pending.clear()
        provider_batch._cancelled.clear()
        try:
            future = provider_batch.submit([{"role": "user", "content": "sent"}],
                                           {"batch_id": "b1", "llm_batch_size": 1})
            assert in_flight.wait(10), "the request never reached the provider"

            started = time.time()
            outcome = provider_batch.cancel("b1")
            assert outcome["in_flight"] == 1
            assert "billed" in outcome["note"]
            with pytest.raises(provider_batch.BatchCancelled):
                future.result(timeout=5)
            assert time.time() - started < 5
        finally:
            release.set()
            time.sleep(.2)
            provider_batch._pending.clear()
            provider_batch._cancelled.clear()

    def test_the_provider_is_asked_to_cancel_when_it_can(self, monkeypatch):
        """And when the API does offer a cancel, it is used."""
        import llm
        from backend.features.execution import provider_batch
        in_flight, release = threading.Event(), threading.Event()
        cancelled = []

        def slow_provider(name, inputs, **kwargs):
            in_flight.set()
            assert release.wait(60)
            return ["ok"] * len(inputs)

        monkeypatch.setattr(llm, "batch", slow_provider, raising=False)
        monkeypatch.setattr(llm, "cancel_batch", lambda name: cancelled.append(name),
                            raising=False)
        provider_batch._pending.clear()
        provider_batch._cancelled.clear()
        try:
            provider_batch.submit([{"role": "user", "content": "sent"}],
                                  {"batch_id": "b1", "llm_batch_size": 1})
            assert in_flight.wait(10)
            outcome = provider_batch.cancel("b1")
            assert cancelled == [provider_batch.PROVIDER]
            assert outcome["provider_cancelled"] is True
        finally:
            release.set()
            time.sleep(.2)
            provider_batch._pending.clear()
            provider_batch._cancelled.clear()

    def test_queued_prompts_are_never_sent(self, provider):
        provider_batch, sent = provider
        futures = [provider_batch.submit([{"role": "user", "content": str(i)}], self.state())
                   for i in range(2)]
        assert sent == []                                # below the flush size

        assert provider_batch.cancel("b1") == {"dropped": 2, "in_flight": 0}
        time.sleep(provider_batch.FLUSH_SECONDS * 6)
        assert sent == []
        assert all(f.cancelled() for f in futures)

    def test_nothing_new_is_accepted_afterwards(self, provider):
        provider_batch, sent = provider
        provider_batch.cancel("b1")
        with pytest.raises(provider_batch.BatchCancelled):
            provider_batch.submit([{"role": "user", "content": "late"}], self.state())
        time.sleep(provider_batch.FLUSH_SECONDS * 6)
        assert sent == []

    def test_another_batch_is_untouched(self, provider):
        provider_batch, sent = provider
        provider_batch.submit([{"role": "user", "content": "keep"}], self.state("b2"))
        provider_batch.cancel("b1")
        assert _wait_until(lambda: sent != [], 5)
        assert sent == [[[{"role": "user", "content": "keep"}]]]

    def test_a_resume_accepts_the_batch_again(self, provider):
        provider_batch, sent = provider
        provider_batch.cancel("b1")
        provider_batch.uncancel("b1")
        provider_batch.submit([{"role": "user", "content": "again"}], self.state())
        assert _wait_until(lambda: sent != [], 5)

    def test_stopping_the_batch_reaches_this_layer(self, provider, batch_store, monkeypatch):
        provider_batch, sent = provider
        from backend.api import runner
        queued = threading.Event()

        def fake_start_run(graph, inputs, background=True, run_id=None, **_kw):
            provider_batch.submit([{"role": "user", "content": "in flight"}],
                                  {"batch_id": batch_id_holder[0], "llm_batch_size": 8})
            queued.set()
            time.sleep(1)
            runner._runs[run_id] = {"run_id": run_id, "status": "success", "result": {},
                                    "nodes": [], "error": None, "review_status": None}
            return run_id

        batch_id_holder = [None]
        monkeypatch.setattr(batch_store.runner, "start_run", fake_start_run)
        monkeypatch.setattr(batch_store.runner, "get_run", lambda rid: runner._runs.get(rid))
        batch_id_holder[0] = batch_store.start_batch(
            {"id": "g"}, [{"n": 1}], {"type": "manual"}, workers=1)

        assert queued.wait(10)
        batch_store.cancel_batch(batch_id_holder[0])
        assert batch_store.wait_for(batch_id_holder[0], timeout=20)
        time.sleep(provider_batch.FLUSH_SECONDS * 6)
        assert sent == []


class TestTheStreamingDatasetReader:
    """A stopped streaming batch has to take its reader process with it.

    The worker holds whatever the Dataset loaded — spaCy, torch, a model on
    disk. It is its own session leader, so it and anything it started go
    together.
    """

    @pytest.fixture
    def resource(self, tmp_path, monkeypatch):
        from backend.features.data import data_resources, dataloaders
        monkeypatch.setattr(data_resources, "data_path",
                            lambda *parts: tmp_path.joinpath(*parts))
        monkeypatch.setattr(dataloaders, "data_path",
                            lambda *parts: tmp_path.joinpath(*parts))
        dataloaders._verified.clear()
        return data_resources.create_from_records([{"i": i} for i in range(10)], "rows")

    def test_flipping_cancelled_kills_the_worker_and_its_group(self, resource, tmp_path):
        from backend.features.data import dataset_stream
        pid_file = tmp_path / "worker.pid"
        # Initialization that takes its time, as loading a model does. The
        # worker writes its pid first so the test can watch the real process.
        code = (
            "import os, time\n"
            "from torch.utils.data import Dataset\n"
            "class Rows(Dataset):\n"
            "    def __len__(self): return 10\n"
            "    def __getitem__(self, index): return {'i': index}\n"
            "def build_dataset(resource):\n"
            f"    open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
            "    time.sleep(300)\n"
            "    return Rows()\n")
        config = {"type": "dataloader", "loader": "python", "resource_id": resource["id"],
                  "code": code, "read_batch_size": 1, "cache": False,
                  "sample_timeout": 300, "batch_timeout": 300}
        stop = threading.Event()
        stream = dataset_stream.chunks(config, stop.is_set)
        pump = threading.Thread(target=lambda: list(stream), daemon=True)
        pump.start()
        try:
            assert _wait_until(lambda: pid_file.is_file(), 60), "the reader never started"
            pid = int(pid_file.read_text())
            assert _alive(pid)

            started = time.time()
            stop.set()                          # what cancel_batch sets
            assert _wait_until(lambda: not _alive(pid), 10), \
                "the reader process outlived the stop"
            assert time.time() - started < 5
            pump.join(10)
            assert not pump.is_alive()
        finally:
            stream.close()
            if pid_file.is_file():
                _kill(int(pid_file.read_text()))

    def test_a_worker_busy_loading_a_model_dies_too(self, resource, tmp_path):
        """A sleeping process is the easy case. This one is inside spaCy,
        loading a pipeline over and over — native code, nothing of ours
        running in it — which is what a real Dataset's initialization is."""
        from backend.features.data import dataset_stream
        pid_file = tmp_path / "spacy.pid"
        code = (
            "import os\n"
            "import spacy\n"
            "from torch.utils.data import Dataset\n"
            "class Rows(Dataset):\n"
            "    def __len__(self): return 10\n"
            "    def __getitem__(self, index): return {'i': index}\n"
            "def build_dataset(resource):\n"
            f"    open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
            "    for _ in range(200):\n"
            "        nlp = spacy.load('en_core_web_sm')\n"
            "        nlp('a sentence to keep the pipeline busy')\n"
            "    return Rows()\n")
        config = {"type": "dataloader", "loader": "python", "resource_id": resource["id"],
                  "code": code, "read_batch_size": 1, "cache": False,
                  "sample_timeout": 600, "batch_timeout": 600}
        stop = threading.Event()
        stream = dataset_stream.chunks(config, stop.is_set)
        pump = threading.Thread(target=lambda: list(stream), daemon=True)
        pump.start()
        try:
            assert _wait_until(lambda: pid_file.is_file(), 120), "the reader never started"
            pid = int(pid_file.read_text())
            started = time.time()
            stop.set()
            assert _wait_until(lambda: not _alive(pid), 10), \
                "the reader survived the stop mid-model-load"
            assert time.time() - started < 5
            pump.join(10)
            assert not pump.is_alive()
        finally:
            stream.close()
            if pid_file.is_file():
                _kill(int(pid_file.read_text()))

    def test_a_stopped_stream_stops_reading_and_settles(self, batch_store, monkeypatch):
        """The whole streamed batch, with a real subprocess behind the reader."""
        from backend.api import runner
        import subprocess
        import sys
        workers = {}

        def chunks(cancelled, on_info=None):
            process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                                       start_new_session=True)
            workers["process"] = process
            try:
                index = 0
                while not cancelled():
                    yield [{"n": index}], None, 1
                    index += 1
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()

        def fake_start_run(graph, inputs, background=True, run_id=None, **_kw):
            runner._runs[run_id] = {"run_id": run_id, "status": "success", "result": inputs,
                                    "nodes": [], "error": None, "review_status": None}
            return run_id

        monkeypatch.setattr(batch_store.runner, "start_run", fake_start_run)
        monkeypatch.setattr(batch_store.runner, "get_run", lambda rid: runner._runs.get(rid))
        batch_id = batch_store.start_batch({"id": "g"}, [], {"type": "canvas"},
                                           workers=1, record_chunks=chunks)
        assert _wait_until(lambda: len(batch_store.get_batch(batch_id)["items"]) >= 3)
        read = len(batch_store.get_batch(batch_id)["items"])

        batch_store.cancel_batch(batch_id)
        assert batch_store.wait_for(batch_id, timeout=20)

        settled = batch_store.get_batch(batch_id)
        assert settled["status"] == "cancelled"
        # Reading stopped: a stream that kept producing would have archived
        # hundreds more records while the batch wound down.
        assert len(settled["items"]) <= read + 2
        # Unread, so Resume can pick the Dataset up where it stopped.
        assert settled["collection_complete"] is False
        assert not _alive(workers["process"].pid), "the reader process survived the stop"


class TestTheSampleButton:
    """Sampling an Input is the same worker under a different button, and the
    only way out of it is the stop route: the request is blocked in the
    worker, so the stop arrives on another one."""

    @pytest.fixture
    def resource(self, tmp_path, monkeypatch):
        from backend.features.data import data_resources, dataloaders
        monkeypatch.setattr(data_resources, "data_path",
                            lambda *parts: tmp_path.joinpath(*parts))
        monkeypatch.setattr(dataloaders, "data_path",
                            lambda *parts: tmp_path.joinpath(*parts))
        dataloaders._verified.clear()
        return data_resources.create_from_records([{"i": i} for i in range(4)], "rows")

    def test_the_stop_route_ends_the_worker_loading_a_model(self, resource, tmp_path):
        from backend.features.data import dataloaders
        from backend.features.chat import chat_control
        pid_file = tmp_path / "sample.pid"
        code = (
            "import os\n"
            "import spacy\n"
            "from torch.utils.data import Dataset\n"
            "class Rows(Dataset):\n"
            "    def __len__(self): return 4\n"
            "    def __getitem__(self, index): return {'i': index}\n"
            "def build_dataset(resource):\n"
            f"    open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
            "    for _ in range(200):\n"
            "        spacy.load('en_core_web_sm')\n"
            "    return Rows()\n")
        config = {"type": "dataloader", "loader": "python", "resource_id": resource["id"],
                  "code": code, "preview_mode": "sample", "cache": False,
                  "sample_timeout": 600, "_request_id": "sample-1"}
        outcome = {}
        asking = threading.Thread(
            target=lambda: outcome.setdefault("error", _call(dataloaders.preview, config)),
            daemon=True)
        asking.start()
        try:
            assert _wait_until(lambda: pid_file.is_file(), 120), "the sample never started"
            pid = int(pid_file.read_text())
            started = time.time()
            assert dataloaders.stop_preview("sample-1") == {"status": "stopping"}
            assert _wait_until(lambda: not _alive(pid), 10), \
                "the sample's worker outlived the stop"
            assert time.time() - started < 5
            asking.join(10)
            assert not asking.is_alive()
            # The request that was sampling comes back said, not hanging.
            assert "409" in outcome["error"]
        finally:
            if pid_file.is_file():
                _kill(int(pid_file.read_text()))
            chat_control._requests.pop((dataloaders.PREVIEW_SCOPE, "sample-1"), None)


class TestTheWatcher:
    """Stopping a watcher must not be followed by the runs it was about to
    start. A poll is a network fetch with its own retries and pauses, so Stop
    routinely lands inside one — and `stop_graph_watch` had already told the
    user the watcher was gone."""

    @pytest.fixture
    def watch(self, tmp_path, monkeypatch):
        from backend.api import watcher
        monkeypatch.setattr(watcher, "WATCH_DIR", tmp_path / "watch")
        monkeypatch.setenv("EAX_WATCH_DEBUG", "1")
        watcher._watchers.clear()
        yield watcher
        watcher.stop_graph_watch("g")

    def graph(self):
        return {"id": "g", "edges": [{"source": "src", "target": "n"}],
                "tasks": [{"name": "src", "kind": "source", "source": {
                    "type": "http_api",
                    "schedule": {"mode": "interval", "interval_minutes": 0.2}}}]}

    def test_no_run_starts_after_stop(self, watch, monkeypatch):
        started = []
        polling = threading.Event()
        release = threading.Event()

        def slow_poll(node, seen, last_poll):
            polling.set()
            assert release.wait(30)
            return [{"a": 1}, {"a": 2}]

        graph = self.graph()
        monkeypatch.setattr(watch, "poll_source", slow_poll)
        monkeypatch.setattr(watch.sources, "find_source_nodes", lambda g: [g["tasks"][0]])
        monkeypatch.setattr(watch.runner, "start_run",
                            lambda g, record, background=True, **kw: (started.append(record), "r")[1])

        assert watch.start_graph_watch(graph)
        assert polling.wait(30), "the watcher never polled"
        assert watch.stop_graph_watch("g") == 1
        release.set()
        time.sleep(1)

        assert started == []
        assert watch.graph_watch_status("g") == {"watching": False, "watchers": []}

    def test_the_records_it_did_not_run_are_not_marked_seen(self, watch, monkeypatch):
        """Otherwise Stop silently drops them: a later start skips them as
        already handled and they are never run at all."""
        polling = threading.Event()
        release = threading.Event()

        def slow_poll(node, seen, last_poll):
            polling.set()
            assert release.wait(30)
            seen["seen"].append("record-1")
            return [{"a": 1}]

        graph = self.graph()
        monkeypatch.setattr(watch, "poll_source", slow_poll)
        monkeypatch.setattr(watch.sources, "find_source_nodes", lambda g: [g["tasks"][0]])
        monkeypatch.setattr(watch.runner, "start_run", lambda *a, **k: "r")

        watch.start_graph_watch(graph)
        assert polling.wait(30)
        watch.stop_graph_watch("g")
        release.set()
        time.sleep(1)

        assert watch._load_seen("g", "src")["seen"] == []


class TestASourceCollection:
    """Stop on a collection: the worker dies, the batch in flight is stopped,
    and the next one is never launched."""

    @pytest.fixture
    def collection(self, tmp_path, monkeypatch):
        from backend.features.data import source_collection
        monkeypatch.setattr(source_collection, "directory", lambda: tmp_path / "collections")
        (tmp_path / "collections").mkdir(parents=True, exist_ok=True)
        return source_collection

    def test_the_batch_after_the_stop_is_never_started(self, collection, monkeypatch):
        from backend.api import chat_control
        launched = []
        first_running = threading.Event()

        def start_batch(graph, records, source, **kw):
            launched.append(records)
            first_running.set()
            return f"batch-{len(launched)}"

        monkeypatch.setattr(collection.batch, "start_batch", start_batch)
        monkeypatch.setattr(collection.batch, "wait_for", lambda bid, timeout=None: False)
        monkeypatch.setattr(collection.batch, "cancel_batch", lambda bid: {"cancelled": True})
        monkeypatch.setattr(collection.batch, "get_batch",
                            lambda bid: {"status": "cancelled", "items": []})
        monkeypatch.setattr(collection, "mapped_chunk",
                            lambda graph, node, records, metric, key: (list(records), None))
        monkeypatch.setattr(collection.preprocess, "apply_dataset",
                            lambda tool, records: list(records))
        monkeypatch.setattr(chat_control, "worker",
                            lambda kind, payload, **kw: [{"r": i} for i in range(6)])

        job = {"id": "c1", "graph_id": "g", "mode": "prepare", "records": [], "batches": [],
               "record_count": 0, "submitted_records": 0, "batch_size": 2, "status": "collecting",
               "collection_complete": False, "phase": "collecting", "preprocess_tool": "t",
               "config": {}, "reference_inputs": {}, "error": None}
        node = {"name": "src", "outputs": [], "source": {"type": "http_api"}}
        control = chat_control.Control()
        worker = threading.Thread(
            target=collection.execute_collection,
            args=(job, {"id": "g"}, node, control, 1, None, None), daemon=True)
        worker.start()

        assert first_running.wait(10)
        control.event.set()                      # what the stop endpoint does
        worker.join(15)
        assert not worker.is_alive()

        # Six records at two per batch is three batches; only the first ran.
        assert len(launched) == 1
        assert job["status"] == "cancelled"
        assert "Stopped" in job["error"]

    def test_the_batch_it_had_launched_is_stopped_with_it(self, collection, monkeypatch):
        """Not only left to finish: the records of the batch in flight are
        the ones a stop is meant to save."""
        from backend.api import chat_control
        cancelled = []
        first_running = threading.Event()

        monkeypatch.setattr(collection.batch, "start_batch",
                            lambda graph, records, source, **kw: (first_running.set(), "b1")[1])
        monkeypatch.setattr(collection.batch, "wait_for", lambda bid, timeout=None: False)
        monkeypatch.setattr(collection.batch, "cancel_batch",
                            lambda bid: (cancelled.append(bid), {"cancelled": True})[1])
        monkeypatch.setattr(collection.batch, "get_batch",
                            lambda bid: {"status": "cancelled", "items": []})
        monkeypatch.setattr(collection, "mapped_chunk",
                            lambda graph, node, records, metric, key: (list(records), None))
        monkeypatch.setattr(collection.preprocess, "apply_dataset",
                            lambda tool, records: list(records))
        monkeypatch.setattr(chat_control, "worker",
                            lambda kind, payload, **kw: [{"r": i} for i in range(4)])

        job = {"id": "c2", "graph_id": "g", "mode": "prepare", "records": [], "batches": [],
               "record_count": 0, "submitted_records": 0, "batch_size": 2, "status": "collecting",
               "collection_complete": False, "phase": "collecting", "preprocess_tool": "t",
               "config": {}, "reference_inputs": {}, "error": None}
        node = {"name": "src", "outputs": [], "source": {"type": "http_api"}}
        control = chat_control.Control()
        worker = threading.Thread(
            target=collection.execute_collection,
            args=(job, {"id": "g"}, node, control, 1, None, None), daemon=True)
        worker.start()
        assert first_running.wait(10)
        control.event.set()
        worker.join(15)

        assert cancelled == ["b1"]
        assert job["status"] == "cancelled"

    def test_a_stopped_collection_still_reaches_a_final_status(self, collection, monkeypatch):
        """Its last act is to evaluate what ran — in a worker process, under
        the same stop that just ended the collection. That worker refusing to
        start must not leave the job reading `collecting` for ever."""
        from backend.api import chat_control
        first_running = threading.Event()

        monkeypatch.setattr(collection.batch, "start_batch",
                            lambda graph, records, source, **kw: (first_running.set(), "b1")[1])
        monkeypatch.setattr(collection.batch, "wait_for", lambda bid, timeout=None: False)
        monkeypatch.setattr(collection.batch, "cancel_batch", lambda bid: {"cancelled": True})
        monkeypatch.setattr(collection.batch, "get_batch",
                            lambda bid: {"status": "cancelled", "items": [
                                {"status": "success", "run_id": None, "inputs": {}}]})
        monkeypatch.setattr(collection, "mapped_chunk",
                            lambda graph, node, records, metric, key: (list(records), None))
        monkeypatch.setattr(collection.preprocess, "apply_dataset",
                            lambda tool, records: list(records))
        # Only the source's own worker is a stand-in; the evaluator below runs
        # in the real one, which is the process this test is about.
        real_worker = chat_control.worker
        monkeypatch.setattr(chat_control, "worker",
                            lambda kind, payload, **kw: ([{"r": i} for i in range(4)]
                                                         if kind == "collect"
                                                         else real_worker(kind, payload, **kw)))

        graph = {"id": "g", "tasks": [
            {"name": "check", "kind": "evaluator", "enabled": True,
             "evaluator": {"type": "python", "timing": "batch", "metric": "score",
                           "code": "def evaluate(records):\n"
                                   "    return {'metrics': {'score': len(records)}}\n"}}]}
        job = {"id": "c3", "graph_id": "g", "mode": "prepare", "records": [], "batches": [],
               "record_count": 0, "submitted_records": 0, "batch_size": 2, "status": "collecting",
               "collection_complete": False, "phase": "collecting", "preprocess_tool": "t",
               "config": {}, "reference_inputs": {}, "error": None}
        node = {"name": "src", "outputs": [], "source": {"type": "http_api"}}
        control = chat_control.Control()
        worker = threading.Thread(
            target=collection.execute_collection,
            args=(job, graph, node, control, 1, None, None), daemon=True)
        worker.start()
        assert first_running.wait(10)
        control.event.set()
        worker.join(30)
        assert not worker.is_alive()

        assert job["status"] == "cancelled"
        # The report over what did run is still worth having.
        assert job["evaluations"]["check"]["status"] == "success"
        assert collection._jobs.get("c3") is None


class TestAWorkerStudioStarted:
    """Every isolated piece of work — a Dataset, an evaluator, a preprocessing
    tool — runs in a worker process of its own session. A stop takes the
    session, so whatever the user's code started goes with it."""

    def test_the_control_takes_the_worker_and_its_children(self, tmp_path):
        from backend.features.chat import chat_control
        pid_file = tmp_path / "pids"
        code = (
            "import os, subprocess, sys, time\n"
            "\n"
            "\n"
            "def evaluate(records):\n"
            "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
            f"    open({str(pid_file)!r}, 'w').write(f'{{os.getpid()}} {{child.pid}}')\n"
            "    time.sleep(300)\n"
            "    return {'metrics': {'score': 1}}\n")
        control = chat_control.Control()
        outcome = {}

        def ask():
            token = chat_control.current.set(control)
            try:
                outcome["error"] = _call(
                    chat_control.worker, "evaluate_python",
                    {"code": code, "records": [{"inputs": {}}], "config": {}})
            finally:
                chat_control.current.reset(token)

        asking = threading.Thread(target=ask, daemon=True)
        asking.start()
        pids = []
        try:
            assert _wait_until(lambda: pid_file.is_file(), 60), "the worker never ran"
            time.sleep(.2)
            pids = [int(value) for value in pid_file.read_text().split()]
            assert all(_alive(pid) for pid in pids)

            started = time.time()
            control.event.set()
            assert _wait_until(lambda: not any(_alive(pid) for pid in pids), 10), \
                "the worker or its child outlived the stop"
            assert time.time() - started < 5
            asking.join(10)
            assert not asking.is_alive()
            assert outcome["error"].startswith("Cancelled")
        finally:
            for pid in pids:
                _kill(pid)


class _StubLLM:
    """Enough of a model for the engine to build its agents around."""

    def __init__(self):
        from evoagentx.models import LiteLLMConfig
        self.config = LiteLLMConfig(model="deepseek/deepseek-chat", deepseek_key="test-only")


def _neutralise_memory(runner, monkeypatch):
    """No long-term memory, no tools: this file is about stopping, and a
    memory write after Stop is its own test elsewhere."""
    from backend.api import tools_registry
    monkeypatch.setattr(runner, "_prepare_ltm",
                        lambda doc, ordered, inputs, state: ({}, ordered))
    monkeypatch.setattr(runner, "_attach_ltm", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_save_ltm", lambda *a, **k: None)
    monkeypatch.setattr(tools_registry, "validate_tool_names", lambda names: None)
    monkeypatch.setattr(tools_registry, "resolve_tools", lambda names, **kw: None)


def _call(function, *args):
    """How a call ended, whichever way it ended: a thread cannot raise into
    the test that started it, and a stop arrives as `Cancelled`, which is not
    an `Exception`."""
    try:
        function(*args)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        return f"{type(exc).__name__} {getattr(exc, 'status_code', '')} {exc}".strip()
    return ""


def _alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _kill(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass
