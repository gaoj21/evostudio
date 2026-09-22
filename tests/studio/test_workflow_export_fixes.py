"""Regression tests for workflow / memory / workspace / export fixes.

Each class pins one bug: exported projects running folder and class-based
tools, exported memory writes using each node's own inputs, canvas saves
keeping the user's workspace files, deleted workflows not leaking their data
into a new one, export/import round trips, short-term memory under concurrent
access, renames through the ordinary save, and table-memory time cutoffs.
"""

import asyncio
import io
import json
import os
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

import pytest

from conftest import make_task

REPO = Path(__file__).resolve().parents[2]

FACTORY_CODE = '''"""Multiply numeric inputs."""
INPUT_SCHEMA = [{"name":"value","type":"float"}]
OUTPUT_SCHEMA = [{"name":"total","type":"float"}]
class Multiply:
    def __init__(self, factor): self.factor = factor
    def run(self, inputs): return {"total": inputs["value"] * self.factor}
def build_tool(factor: float = 2.0): return Multiply(factor)
'''

PACKAGE_ENTRY = ('"""Doubler."""\nfrom helper import twice\n\n'
                 'def doubler(text: str) -> str:\n    """Double it.\n\n    Args:\n'
                 '        text: input\n    """\n    return twice(text) + open("suffix.txt").read()\n')


def _package_zip(entry=PACKAGE_ENTRY) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("pkgtool/helper.py", "def twice(s):\n    return s + s\n")
        z.writestr("pkgtool/suffix.txt", "!")
        z.writestr("pkgtool/tools.py", entry)
    return buf.getvalue()


def _tool_node(name, tool, inputs, outputs, types=("str", "str")):
    return {"name": name, "kind": "tool", "tool": tool, "enabled": True,
            "inputs": [{"name": n, "type": types[0]} for n in inputs],
            "outputs": [{"name": n, "type": types[1]} for n in outputs]}


def _write_project(files, binary, out: Path):
    for rel, content in files.items():
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_text(content, encoding="utf-8")
    for rel, blob in binary.items():
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_bytes(blob)


def _run_in_project(out: Path, driver: str) -> str:
    (out / "driver.py").write_text(driver, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "driver.py"], cwd=out, capture_output=True, text=True, timeout=300,
        # The framework comes from the checkout rather than a vendored copy,
        # which keeps the test from writing 262 files it does not look at.
        env={**os.environ, "PYTHONPATH": str(REPO / "backend")})
    assert proc.returncode == 0, proc.stderr[-3000:]
    return proc.stdout


class TestExportedCustomTools:
    """Bug 1: folder toolkits and class-based tools run in an exported project."""

    def test_folder_and_factory_tools_run_like_studio(self, studio_data, tmp_path):
        from backend.api import custom_tools, export_api, graphs, tools_registry
        builtins = tools_registry.builtin_names()
        custom_tools.unpack_toolkit(_package_zip(), None, None, [], builtins)
        cache = custom_tools.package_dir("pkgtool") / "__pycache__"
        cache.mkdir()
        (cache / "helper.cpython-312.pyc").write_bytes(b"\0")
        custom_tools.save_custom_tool(custom_tools.validate_spec(
            {"code": FACTORY_CODE, "name": "multiply", "config": {"factor": 5.0}}, builtins))
        assert custom_tools.run_custom_tool("doubler", {"text": "ab"}) == {"result": "abab!"}

        graph = {"id": "exptools", "name": "exptools", "goal": "g", "flow_version": 2,
                 "tasks": [_tool_node("T", "doubler", ["text"], ["doubled"]),
                           _tool_node("M", "multiply", ["value"], ["total"], ("float", "float"))],
                 "edges": []}
        graphs.validate_graph(graph)
        files, binary = export_api.project_files(graph, include_vendor=False)
        assert not [p for p in [*files, *binary] if "__pycache__" in p]
        assert json.loads(files["tools/multiply.json"])["config"] == {"factor": 5.0}

        out = tmp_path / "project"
        _write_project(files, binary, out)
        stdout = _run_in_project(out, '''
import json, sys
sys.path.insert(0, ".")
import workflow
nodes = {t["name"]: t for t in workflow.TOOL_NODES}
print(json.dumps([workflow.call_tool_node(nodes["T"], {"text": "ab"}),
                  workflow.call_tool_node(nodes["M"], {"value": 3.0})]))
''')
        assert json.loads(stdout.strip().splitlines()[-1]) == [
            {"doubled": "abab!"}, {"total": 15.0}]


class TestExportedMemoryWrites:
    """Bug 2: an exported node remembers its own inputs and outputs."""

    def test_mapped_inputs_and_shadowed_outputs(self, studio_data, tmp_path):
        from backend.api import export_api, graphs
        mem = {"version": 2, "kind": "table", "write_mode": "append",
               "inputs": ["text"], "outputs": ["verdict"]}
        graph = {"id": "expmem", "name": "expmem", "goal": "g", "flow_version": 2,
                 "tasks": [make_task("A", ["topic"], ["summary"], enabled=True),
                           make_task("B", ["text"], ["verdict"], enabled=True,
                                     use_long_term_memory=True, memory=mem),
                           make_task("C", ["v"], ["verdict"], enabled=True)],
                 "edges": [{"source": "A", "target": "B",
                            "mappings": [{"from": "summary", "to": "text"}]},
                           {"source": "B", "target": "C",
                            "mappings": [{"from": "verdict", "to": "v"}]}]}
        graphs.validate_graph(graph)
        files, binary = export_api.project_files(graph, include_vendor=False)
        out = tmp_path / "project"
        _write_project(files, binary, out)
        stdout = _run_in_project(out, '''
import json, sys, types
sys.path.insert(0, ".")
import workflow
from evoagentx.models import LiteLLMConfig
workflow.load_llm = lambda: types.SimpleNamespace(
    config=LiteLLMConfig(model="deepseek/deepseek-chat", deepseek_key="test-only"))
async def fake(agent, task, inputs):
    return {"A": {"summary": "S-" + inputs.get("topic", "")},
            "B": {"verdict": "OK:" + inputs.get("text", "")},
            "C": {"verdict": "C-OVERWRITE"}}[task["name"]]
workflow._run_llm = fake
workflow.run({"topic": "acme"})
import table_store
table_store.TABLES_DIR = workflow.HERE / "memory_tables"
print(json.dumps([r["payload"] for r in table_store.rows(workflow.GRAPH_ID, "B")]))
''')
        stored = json.loads(stdout.strip().splitlines()[-1])
        assert len(stored) == 1
        assert stored[0]["inputs"] == {"text": "S-acme"}
        assert stored[0]["outputs"] == {"verdict": "OK:S-acme"}


class TestWorkspaceSaveKeepsUserFiles:
    """Bug 3: a canvas save removes only what the previous generation wrote."""

    def test_user_files_survive_and_stale_generated_files_go(self, studio_data):
        from backend.api import custom_tools, graphs, tools_registry, workspace
        custom_tools.save_custom_tool(custom_tools.validate_spec(
            {"code": FACTORY_CODE, "name": "multiply"}, tools_registry.builtin_names()))
        g = graphs.create_graph("W", "g")
        g = graphs.save_graph(g["id"], {
            "name": "W", "goal": "g", "flow_version": 2, "edges": [],
            "tasks": [_tool_node("M", "multiply", ["value"], ["total"], ("float", "float"))]})
        workspace.write_project(g)
        assert (workspace.workspace_root(g["id"]) / "tools/multiply.json").is_file()
        workspace.write_file(g["id"], "notes.md", b"my notes")
        workspace.write_file(g["id"], "data/customers.csv", b"a,b\n1,2")
        workspace.write_file(g["id"], "tools/mine.py", b"# mine")

        g = graphs.save_graph(g["id"], {"name": "W", "goal": "g", "flow_version": 2, "edges": [],
                                        "tasks": [make_task("A", ["topic"], ["out"], enabled=True)]})
        workspace.write_project(g)
        root = workspace.workspace_root(g["id"])
        for kept in ("notes.md", "data/customers.csv", "tools/mine.py", "workflow.py"):
            assert (root / kept).is_file(), kept
        # The tool the workflow no longer uses was generated, so it goes.
        assert not (root / "tools/multiply.json").exists()
        assert workspace.GENERATED_MANIFEST not in {e["path"] for e in workspace.tree(g["id"])}

    @pytest.mark.parametrize("folder", ["data", "tools/out", "skills", "vendor"])
    def test_output_dir_in_a_generated_folder_is_refused(self, studio_data, folder):
        from backend.api import graphs
        g = graphs.create_graph("W", "g")
        with pytest.raises(graphs.GraphValidationError):
            graphs.save_graph(g["id"], {"name": "W", "goal": "g", "tasks": [], "edges": [],
                                        "output_dir": folder})


class TestDeletedWorkflowData:
    """Bug 4: a deleted workflow's data is archived, never inherited."""

    def test_a_new_workflow_of_the_same_name_starts_empty(self, studio_data):
        from backend.api import graphs, table_store
        g = graphs.create_graph("Foo", "")
        table_store.write(g["id"], "n", "acme", "2024-01-01", {"v": "old"}, "t")
        graphs.delete_graph(g["id"])
        again = graphs.create_graph("Foo", "")
        assert again["id"] == "foo"
        assert table_store.rows("foo", "n") == []
        archived = list(table_store.TABLES_DIR.glob(".deleted-*/foo/n.db"))
        assert len(archived) == 1      # set aside, not removed

    def test_leftover_data_counts_as_taken(self, studio_data):
        from backend.api import graphs, table_store
        # Data left by a workflow deleted before archiving existed.
        table_store.write("baz", "n", "x", "2024-01-01", {"v": "stale"}, "t")
        assert graphs.create_graph("Baz", "")["id"] == "baz-2"
        bar = graphs.create_graph("Bar", "")
        table_store.write("bar", "n", "y", "2024-01-01", {"v": "bar own"}, "t")
        renamed = graphs.rename_graph(bar["id"], "Baz")
        assert renamed["id"] == "baz-3"
        assert [r["payload"] for r in table_store.rows("baz-3", "n")] == [{"v": "bar own"}]
        assert [r["payload"] for r in table_store.rows("baz", "n")] == [{"v": "stale"}]
        assert table_store.rows("bar", "n") == []


def _roundtrip(graph):
    from backend.api import custom_tools, export_api
    from backend.features.workspace import project_import
    name, blob = export_api.build_project(graph)
    for path in list(custom_tools.TOOLS_DIR.glob("*.json")):
        custom_tools.delete_custom_tool(path.stem)

    class Upload:
        filename = name

        async def read(self):
            return blob
    return asyncio.run(project_import.import_graph(Upload()))


class TestExportImportRoundTrip:
    """Bugs 5 and 6: disabled nodes and full tool specs survive the trip."""

    def test_disabled_node_comes_back_and_the_graph_is_valid(self, studio_data):
        from backend.api import graphs
        graph = {"id": "rt", "name": "rt", "goal": "g", "flow_version": 2,
                 "tasks": [make_task("A", ["topic"], ["summary"], enabled=True),
                           make_task("B", ["summary"], ["out"], enabled=False)],
                 "edges": [{"source": "A", "target": "B",
                            "mappings": [{"from": "summary", "to": "summary"}]}]}
        graphs.validate_graph(graph)
        result = _roundtrip(graph)
        assert {t["name"]: t.get("enabled", True) for t in result["tasks"]} == {"A": True, "B": False}
        graphs.validate_graph(graphs.load_graph(result["id"]))

    def test_invalid_import_is_refused_before_saving(self, studio_data):
        from fastapi import HTTPException

        from backend.api import graphs
        from backend.features.workspace import project_import
        payload = json.dumps({"name": "broken", "goal": "", "flow_version": 2,
                              "tasks": [make_task("A", ["topic"], ["summary"], enabled=True)],
                              "edges": [{"source": "A", "target": "Ghost"}]}).encode()

        class Upload:
            filename = "graph.json"

            async def read(self):
                return payload
        with pytest.raises(HTTPException) as err:
            asyncio.run(project_import.import_graph(Upload()))
        assert err.value.status_code == 422
        assert graphs.list_graphs() == []

    def test_tool_config_sources_and_folders_are_restored(self, studio_data):
        from backend.api import custom_tools, tools_registry
        builtins = tools_registry.builtin_names()
        custom_tools.save_custom_tool(custom_tools.validate_spec(
            {"code": FACTORY_CODE, "name": "multiply", "config": {"factor": 5.0},
             "requirements": ["requests"]}, builtins))
        custom_tools.unpack_toolkit(_package_zip(), None, None, ["doubler"], builtins)
        graph = {"id": "rt2", "name": "rt2", "goal": "g", "flow_version": 2, "edges": [],
                 "tasks": [_tool_node("M", "multiply", ["value"], ["total"], ("float", "float")),
                           _tool_node("T", "doubler", ["text"], ["doubled"])]}
        result = _roundtrip(graph)
        assert sorted(result["imported_tools"]) == ["multiply", "pkgtool"], result["notes"]
        restored = custom_tools._read_spec("multiply")
        assert restored["config"] == {"factor": 5.0}
        assert restored["requirements"] == ["requests"]
        assert custom_tools.run_custom_tool("multiply", {"value": 3.0}) == {"result": {"total": 15.0}}
        package = custom_tools._read_spec("pkgtool")
        assert package["sources"] == ["doubler"] and package["package"]["entry"] == "tools.py"
        assert custom_tools.run_custom_tool("doubler", {"text": "ab"}) == {"result": "abab!"}
        # The runner node names its tool only; export-time extras stay out.
        assert "toolkit" not in next(t for t in result["tasks"] if t["name"] == "T")


class TestShortTermMemoryConcurrency:
    """Bug 7: a reader never sees a session log empty while it is rewritten."""

    def test_reads_during_writes_never_come_back_empty(self, studio_data):
        from backend.api import stm_store
        for _ in range(60):
            stm_store.append("g", "s", {"node": "n", "inputs": {"x": "y" * 2000}})
        empties = []

        def reader():
            for _ in range(400):
                if not stm_store.recent("g", "s", 5):
                    empties.append(1)

        def writer():
            for _ in range(40):
                stm_store.append("g", "s", {"node": "n", "inputs": {"x": "z" * 2000}})
        threads = [threading.Thread(target=reader), threading.Thread(target=writer)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert empties == []
        assert not list(stm_store.STM_DIR.glob("*.tmp"))


def _put_body(name, **extra):
    return {"name": name, "goal": "g", "flow_version": 2,
            "tasks": [make_task("A", ["text"], ["summary"], enabled=True),
                      make_task("B", ["summary"], ["out"], enabled=True)],
            "edges": [{"source": "A", "target": "B", "control_only": True}], **extra}


class TestSavingThroughTheApi:
    """Bugs 8 and 9: the PUT route renames safely and reports true inputs."""

    @pytest.fixture
    def client(self, studio_data):
        from fastapi.testclient import TestClient

        from backend.api import app as studio_app
        return TestClient(studio_app.app)

    def test_rename_by_save_moves_the_schedule(self, client):
        from backend.api import graphs, scheduler
        created = graphs.create_graph("Untitled", "")
        scheduler._save(created["id"], {"enabled": False, "mode": "interval"})
        res = client.put(f"/api/graphs/{created['id']}", json=_put_body("Renamed"))
        assert res.status_code == 200 and res.json()["id"] == "renamed"
        assert scheduler.load("renamed") is not None
        assert scheduler.load(created["id"]) is None

    def test_a_refused_save_does_not_rename(self, client):
        from backend.api import graphs, table_store
        created = graphs.create_graph("Untitled", "")
        table_store.write(created["id"], "n", "s", "2024-01-01", {"v": 1}, "t")
        res = client.put(f"/api/graphs/{created['id']}",
                         json=_put_body("Renamed", output_dir="../outside"))
        assert res.status_code == 422
        assert graphs.graph_exists(created["id"]) and not graphs.graph_exists("renamed")
        assert table_store.rows(created["id"], "n")

    def test_reported_inputs_match_the_stored_graph(self, client):
        from backend.api import graphs
        created = graphs.create_graph("G", "")
        put = client.put(f"/api/graphs/{created['id']}", json=_put_body("G"))
        get = client.get(f"/api/graphs/{created['id']}")
        names = lambda r: sorted(i["name"] for i in r.json()["workflow_inputs"])  # noqa: E731
        assert names(put) == names(get) == ["summary", "text"]


class TestFolderToolkitCodeEdits:
    """Bug 10: a folder toolkit's advertised tools always match its code."""

    EDITED = ('"""Doubler."""\n\ndef doubler(text: str) -> str:\n    """Double it."""\n'
              '    return text + text\n\ndef tripler(text: str) -> str:\n'
              '    """Triple it."""\n    return text * 3\n')

    def test_resave_rebuilds_from_the_kept_code(self, studio_data):
        from backend.api import custom_tools, tools_registry
        builtins = tools_registry.builtin_names()
        custom_tools.unpack_toolkit(_package_zip(), None, None, [], builtins)
        saved = custom_tools.save_custom_tool(
            custom_tools.validate_spec({"code": self.EDITED, "name": "pkgtool"}, builtins))
        assert [t["name"] for t in saved["tools"]] == ["doubler"]
        assert "from helper import twice" in saved["code"]

    def test_the_route_refuses_a_code_edit(self, studio_data):
        from fastapi.testclient import TestClient

        from backend.api import app as studio_app
        from backend.api import custom_tools, tools_registry
        custom_tools.unpack_toolkit(_package_zip(), None, None, [], tools_registry.builtin_names())
        client = TestClient(studio_app.app)
        res = client.post("/api/tools/custom", json={"name": "pkgtool", "code": self.EDITED})
        assert res.status_code == 422 and "folder" in res.json()["detail"]
        same = client.post("/api/tools/custom", json={
            "name": "pkgtool", "code": custom_tools._read_spec("pkgtool")["code"],
            "sources": ["doubler"]})
        assert same.status_code == 200 and same.json()["sources"] == ["doubler"]


class TestTableTimeCutoff:
    def test_last_microsecond_before_a_cutoff_is_before_it(self, studio_data):
        from backend.api import table_store
        from backend.features.memory.bindings import timestamp
        for at in ("2024-02-29T23:59:59.999999+00:00", "2024-03-01T00:00:00+00:00"):
            table_store.write("g", "n", "acme", timestamp(at), {"at": at}, "r")
        cutoff = timestamp("2024-03-01")
        assert [r["payload"]["at"] for r in table_store.before("g", "n", "acme", cutoff, 10)] \
            == ["2024-02-29T23:59:59.999999+00:00"]
        assert len(table_store.latest("g", "n", 10, cutoff)) == 1
