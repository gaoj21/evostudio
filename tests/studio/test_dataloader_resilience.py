"""DataLoader runs that survive failures, stops and restarts, on any data.

Nothing here is about a particular task: records are {id, group, value, day}.
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api import batch, graphs, runner
from backend.features.data import data_resources, dataloaders, source_apis
from backend.features.execution import loader_run

CODE = '''import json
from torch.utils.data import IterableDataset
OUTPUT_SCHEMA = [{"name": "id", "type": "int"}, {"name": "group", "type": "str"},
                 {"name": "value", "type": "int"}, {"name": "day", "type": "str"}]
class Rows(IterableDataset):
    def __init__(self, path): self.path = path
    def __iter__(self):
        with open(self.path) as f:
            for line in f:
                if line.strip(): yield json.loads(line)
def build_dataset(resource):
    return Rows(resource["root"] + "/" + resource["files"][0]["path"])
'''

ROWS = [{"id": i, "group": "a" if i % 2 == 0 else "b", "value": i, "day": f"2026-01-{i + 1:02d}"} for i in range(8)]


@pytest.fixture
def env(tmp_path, monkeypatch):
    from evoagentx.models.model_configs import LiteLLMConfig
    monkeypatch.setattr(data_resources, "data_path", lambda *p: tmp_path.joinpath(*p))
    monkeypatch.setattr(batch, "BATCHES_DIR", tmp_path / "batches")
    from backend.features.memory import table_store
    monkeypatch.setattr(table_store, "TABLES_DIR", tmp_path / "tables")
    table_store.forget()
    batch._batches.clear(); batch._threads.clear()
    resource = data_resources.create_from_records(ROWS, "rows")
    behaviour = {"fail": set(), "delay": 0.0, "seen": []}

    async def fake_llm(agent, task, inputs, state):
        value = int(inputs["value"])          # prompt inputs arrive as text
        behaviour["seen"].append(value)
        await asyncio.sleep(behaviour["delay"])
        if value in behaviour["fail"]:
            raise ValueError("the model returned nothing usable")
        return {"verdict": f"ok {inputs['value']}"}

    class StubLLM:
        config = LiteLLMConfig(model="deepseek/deepseek-chat", deepseek_key="test-only")
    monkeypatch.setattr(runner, "execute_llm_node", fake_llm)
    monkeypatch.setattr(runner, "_make_llm", lambda **kw: StubLLM())
    graph = {"id": "resilience", "name": "Resilience", "goal": "g", "flow_version": 2, "tasks": [
        {"name": "input", "kind": "source",
         "source": {"type": "dataloader", "loader": "python", "resource_id": resource["id"], "code": CODE,
                    "read_batch_size": 2, "n": 0},
         "inputs": [], "outputs": [{"name": n, "type": "str", "required": True} for n in ("id", "group", "value", "day")]},
        {"name": "judge", "description": "d", "prompt": "{group}: {value}", "parse_mode": "json",
         "inputs": [{"name": "group", "type": "str", "required": True}, {"name": "value", "type": "int", "required": True}],
         "outputs": [{"name": "verdict", "type": "str", "required": True}],
         "use_long_term_memory": True,
         "memory": {"version": 2, "kind": "table", "write_mode": "append", "time_filter": False, "match": "group"}}],
        "edges": [{"source": "input", "target": "judge",
                   "mappings": [{"from": "group", "to": "group"}, {"from": "value", "to": "value"}]}]}
    return graph, behaviour


def values(state):
    return [json.loads((batch.BATCHES_DIR / i["input_file"]).read_text())["value"] for i in state["items"]]


class TestAFailureDoesNotStopTheStream:
    def test_only_the_failed_records_group_is_held_back(self, env):
        graph, behaviour = env
        behaviour["fail"].add(2)
        result = loader_run.start(graph, graph["tasks"][0], {"workers": 1})
        assert batch.wait_for(result["batch_id"], timeout=30)
        state = batch.get_batch(result["batch_id"])
        assert state["collection_complete"] and len(state["items"]) == 8
        by_value = dict(zip(values(state), (i["status"] for i in state["items"])))
        # group a: 0 ok, 2 failed, 4 and 6 wait for it; group b runs throughout
        assert by_value == {0: "success", 1: "success", 2: "failed", 3: "success",
                            4: "blocked", 5: "success", 6: "blocked", 7: "success"}
        assert state["status"] == "completed_with_errors"

    def test_resume_runs_the_held_back_records_in_order(self, env):
        graph, behaviour = env
        behaviour["fail"].add(2)
        bid = loader_run.start(graph, graph["tasks"][0], {"workers": 1})["batch_id"]
        assert batch.wait_for(bid, timeout=30)
        behaviour["fail"].clear(); behaviour["seen"].clear()
        outcome = batch.resume_batch(bid, graph)
        assert outcome["resumed"] and outcome["continues_reading"] is False
        assert batch.wait_for(bid, timeout=30)
        state = batch.get_batch(bid)
        assert behaviour["seen"] == [2, 4, 6]
        assert state["status"] == "succeeded" and len(state["items"]) == 8


class TestAStoppedStreamContinuesWhereItStopped:
    def test_resume_reads_the_rest_without_repeating_records(self, env):
        graph, behaviour = env
        behaviour["delay"] = 0.4
        bid = loader_run.start(graph, graph["tasks"][0], {"workers": 1})["batch_id"]
        time.sleep(0.6)
        batch.cancel_batch(bid)
        assert batch.wait_for(bid, timeout=30)
        stopped = batch.get_batch(bid)
        assert not stopped["collection_complete"] and len(stopped["items"]) < 8
        assert stopped["unread"] if "unread" in stopped else True
        behaviour["delay"] = 0.0
        outcome = batch.resume_batch(bid, graph)
        assert outcome["resumed"] and outcome["continues_reading"] is True
        assert batch.wait_for(bid, timeout=30)
        state = batch.get_batch(bid)
        assert sorted(values(state)) == list(range(8))           # every record once
        assert state["collection_complete"] and state["status"] == "succeeded"

    def test_the_listing_offers_resume_for_an_unread_stream(self, env):
        graph, behaviour = env
        behaviour["delay"] = 0.4
        bid = loader_run.start(graph, graph["tasks"][0], {"workers": 1})["batch_id"]
        time.sleep(0.6); batch.cancel_batch(bid); assert batch.wait_for(bid, timeout=30)
        row = next(b for b in batch.list_batches("resilience") if b["batch_id"] == bid)
        assert row["streaming"] and row["unread"]


class TestGroupingByPeriodNamesItsField:
    def test_a_period_needs_an_explicit_date_field(self):
        from backend.api.sources import SourceError
        with pytest.raises(SourceError, match="Choose the date field"):
            loader_run.period_settings({"period": "weekly"})
        assert loader_run.period_settings({"period": "weekly", "date_field": "day"}) == ("weekly", "day")
        assert loader_run.period_settings({}) == ("none", "")


class TestApiSourcesAreCollectedNotFetchedInline:
    def api_graph(self):
        return {"id": "api-loader", "name": "API", "goal": "g", "flow_version": 2, "tasks": [
            {"name": "input", "kind": "source",
             "source": {"type": "dataloader", "loader": "source", "read_batch_size": 10, "n": 0,
                        "source_config": {"type": "gdelt_news", "query": "anything", "batch_step": "daily"}},
             "outputs": [{"name": "text", "type": "str"}]},
            {"name": "use", "description": "d", "prompt": "{text}", "parse_mode": "str",
             "inputs": [{"name": "text", "type": "str", "required": True}],
             "outputs": [{"name": "out", "type": "str", "required": True}]}],
            "edges": [{"source": "input", "target": "use", "mappings": [{"from": "text", "to": "text"}]}]}

    def test_run_and_preview_ask_for_collection_and_never_fetch(self, monkeypatch):
        from backend.api.app import app
        g = self.api_graph()
        monkeypatch.setattr(graphs, "load_graph", lambda _: g)
        monkeypatch.setattr(source_apis, "collect_records", lambda *a, **k: pytest.fail("fetched inside the request"))
        monkeypatch.setattr(source_apis, "fetch_gdelt_news", lambda *a, **k: pytest.fail("fetched inside the request"))
        client = TestClient(app)
        preview = client.post("/api/graphs/api-loader/run-batch/preview", json={"source": "canvas"})
        assert preview.status_code == 200 and preview.json()["requires_collection"] is True
        assert preview.json()["source"]["type"] == "gdelt_news"
        run = client.post("/api/graphs/api-loader/run-batch", json={"source": "canvas"})
        assert run.status_code == 422 and "Collect it" in run.text

    def test_the_reader_collects_the_whole_range(self, monkeypatch):
        calls = []
        monkeypatch.setattr(source_apis, "collect_records", lambda config, *a, **k: calls.append(config) or
                            [{"text": f"window {i}"} for i in range(3)])
        monkeypatch.setattr(source_apis, "fetch_source_record", lambda *a, **k: pytest.fail("single fetch used"))
        rows, _ = dataloaders.raw_records({"loader": "source", "source_config": {"type": "gdelt_news", "query": "q"}})
        assert [r["text"] for r in rows] == ["window 0", "window 1", "window 2"] and len(calls) == 1

    def test_a_local_source_is_still_read_directly(self, monkeypatch):
        from backend.features.data import sources
        monkeypatch.setattr(sources, "records_from_source_node", lambda node: [{"x": 1}])
        monkeypatch.setattr(source_apis, "collect_records", lambda *a, **k: pytest.fail("collected a local source"))
        rows, _ = dataloaders.raw_records({"loader": "source", "source_config": {"type": "user_dataset", "dataset_id": "d"}})
        assert rows == [{"x": 1}]
        assert not dataloaders.api_backed({"loader": "source", "source_config": {"type": "user_dataset"}})
        assert dataloaders.api_backed({"loader": "source", "source_config": {"type": "http_api"}})


class TestCollectedDataBecomesAResource:
    def test_a_collection_is_saved_as_an_immutable_resource(self, tmp_path, monkeypatch):
        from backend.features.chat import chat_control
        from backend.features.data import source_collection
        monkeypatch.setattr(data_resources, "data_path", lambda *p: tmp_path.joinpath(*p))
        monkeypatch.setattr(source_collection, "directory", lambda: (tmp_path / "collections").mkdir(exist_ok=True) or tmp_path / "collections")
        fetched = [{"text": f"item {i}"} for i in range(4)]

        def fake_worker(kind, payload, on_stage=None, on_record=None, timeout=None):
            for r in fetched:
                on_record(r)
            return fetched
        monkeypatch.setattr(chat_control, "worker", fake_worker)
        node = {"name": "input", "kind": "source", "source": {"type": "http_api", "url": "https://example.test/items"},
                "outputs": [{"name": "text", "type": "str"}]}
        graph = {"id": "g", "tasks": [node], "edges": []}
        job = {"id": "c" * 32, "graph_id": "g", "fingerprint": "f", "preprocess_tool": "", "phase": "collecting",
               "config": node["source"], "reference_inputs": {}, "status": "collecting", "completed": 0, "total": None,
               "record_count": 0, "records": [], "error": None, "mode": "all", "batch_size": 10, "batches": [],
               "submitted_records": 0, "collection_complete": False}
        source_collection.execute_collection(job, graph, node, chat_control.Control(), 1, None, None)
        assert job["status"] == "ready", job.get("error")
        resource = data_resources.load(job["resource_id"])
        body = (data_resources.path_for(resource["id"]) / "files" / "records.jsonl").read_text()
        assert [json.loads(l) for l in body.splitlines()] == fetched
        assert resource["records"] == 4 and resource["origin"]["collection_id"] == job["id"]


class TestPreparationIsNotSerializedAcrossDatasets:
    def test_different_datasets_prepare_at_the_same_time(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data_resources, "data_path", lambda *p: tmp_path.joinpath(*p))
        monkeypatch.setattr(dataloaders, "data_path", lambda *p: tmp_path.joinpath(*p))
        resource = data_resources.create_from_records([{"a": 1}], "r")

        def slow(config):
            time.sleep(0.6)
            return [{"a": 1}], {"snapshot": "s"}
        monkeypatch.setattr(dataloaders, "_prepare", slow)
        configs = [{"resource_id": resource["id"], "read_batch_size": size} for size in (1, 2)]
        threads = [threading.Thread(target=dataloaders.prepare, args=(c,)) for c in configs]
        t0 = time.monotonic(); [t.start() for t in threads]; [t.join() for t in threads]
        assert time.monotonic() - t0 < 1.0


class TestTimePerBatchIsConfigurable:
    @pytest.mark.parametrize("value,ok", [(120, True), (10, True), (3600, True), (5, False), (4000, False), (True, False)])
    def test_bounds(self, value, ok, tmp_path, monkeypatch):
        from backend.api.sources import SourceError
        monkeypatch.setattr(data_resources, "data_path", lambda *p: tmp_path.joinpath(*p))
        resource = data_resources.create_from_records([{"a": 1}], "r")
        config = {"loader": "python", "code": CODE, "resource_id": resource["id"], "batch_timeout": value}
        if ok:
            dataloaders.validate(config)
            assert dataloaders.batch_timeout(config) == value
        else:
            with pytest.raises(SourceError, match="Time allowed per batch"):
                dataloaders.validate(config)


def test_a_worker_exits_when_the_server_that_started_it_is_gone(tmp_path):
    """A streaming Dataset worker waits for its next batch to be taken. If
    the server dies it must not wait (and hold memory) forever."""
    worker = Path(__file__).resolve().parents[2] / "backend/features/chat/chat_worker.py"
    work = tmp_path / "work"; work.mkdir()
    (work / "input.json").write_text(json.dumps({"code": CODE.replace("yield json.loads(line)", "import time; time.sleep(10**6)"),
                                                 "resource": {"root": str(tmp_path), "files": [{"path": "x"}]},
                                                 "config": {}, "batch_size": 1, "offset": 0, "record_limit": 0}))
    (tmp_path / "x").write_text('{"id": 1}\n')
    parent = f"""
import subprocess, sys, os
p = subprocess.Popen([sys.executable, {str(worker)!r}, 'dataset_stream', {str(work)!r}],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                     env={{**os.environ, 'STUDIO_WORKER_PARENT': str(os.getpid())}})
print(p.pid, flush=True)
os._exit(0)
"""
    pid = int(subprocess.run([sys.executable, "-c", parent], capture_output=True, text=True, timeout=30).stdout.split()[0])
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.2)
    os.kill(pid, 9)
    pytest.fail("worker outlived the process that started it")


def test_the_group_by_period_example_runs_on_any_records(tmp_path, monkeypatch):
    """The example shipped in the Dataset editor, run as the editor would."""
    from backend.features.chat import chat_control
    monkeypatch.setattr(data_resources, "data_path", lambda *p: tmp_path.joinpath(*p))
    jsx = (Path(__file__).resolve().parents[2] / "frontend/src/features/data/PythonDatasetEditor.jsx").read_text()
    code = re.search(r"export const AGGREGATE_EXAMPLE = `(.*?)`;", jsx, re.S).group(1).replace("\\\\", "\\")
    rows = [{"who": w, "when": f"2026-03-{d:02d}", "note": f"{w}{d}"} for d in (2, 3, 9, 10) for w in ("x", "y")]
    resource = data_resources.create_from_records(rows, "notes")
    root = str(data_resources.path_for(resource["id"]) / "files")
    items = chat_control.worker("dataset", {
        "code": code, "resource": {**resource, "root": root},
        "config": {"date_field": "when", "entity_field": "who", "text_field": "note", "period": "week"},
        "batch_size": 10, "sample_limit": None, "offset": 0, "record_limit": 0}, timeout=60)
    assert [(i["entity"], i["period_start"], i["period_end"], i["count"]) for i in items] == [
        ("x", "2026-03-02", "2026-03-08", 2), ("y", "2026-03-02", "2026-03-08", 2),
        ("x", "2026-03-09", "2026-03-15", 2), ("y", "2026-03-09", "2026-03-15", 2)]
    assert items[0]["text"] == "[2026-03-02] x2\n[2026-03-03] x3"
    from backend.features.data.dataset_interface import declared_outputs
    assert {f["name"] for f in declared_outputs(code)} == set(items[0])
