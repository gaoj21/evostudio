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
        yield provider_batch, sent
        provider_batch._pending.clear()
        provider_batch._cancelled.clear()

    def state(self, batch_id="b1"):
        return {"batch_id": batch_id, "llm_batch_size": 8}

    def test_queued_prompts_are_never_sent(self, provider):
        provider_batch, sent = provider
        futures = [provider_batch.submit([{"role": "user", "content": str(i)}], self.state())
                   for i in range(2)]
        assert sent == []                                # below the flush size

        assert provider_batch.cancel("b1") == {"dropped": 2}
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
