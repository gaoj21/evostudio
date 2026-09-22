"""A tool can be written the way a DataLoader is: a whole module with its
imports, a class whose public methods are the tools and whose constructor's
typed parameters are its configuration, or an uploaded package -- checked
before saving, test-called, and explained in terms of its own lines when it
fails."""

import asyncio
import io
import json
import threading
import zipfile

import pytest

from backend.api import custom_tools
from backend.api.custom_tools import CustomToolError
from backend.features.library import tool_sessions

COUNTER = '''"""Counts and remembers words."""
import re
from collections import Counter

WORD = re.compile(r"[A-Za-z']+")


def _words(text):
    return WORD.findall(text)


class WordStats:
    """Word statistics over everything it has been shown."""

    def __init__(self, lowercase: bool = True, minimum: int = 1):
        self.lowercase = lowercase
        self.minimum = minimum
        self.seen = Counter()

    def add(self, text: str) -> dict:
        """Count the words of a text and remember them.

        Args:
            text: the text to count
        """
        words = [w.lower() if self.lowercase else w for w in _words(text) if len(w) >= self.minimum]
        self.seen.update(words)
        return {"added": len(words)}

    def top(self, n: int = 3) -> list:
        """The most frequent words seen so far.

        Args:
            n: how many
        """
        return [w for w, _ in self.seen.most_common(n)]

    def _reset(self):
        self.seen.clear()

    @property
    def total(self):
        return sum(self.seen.values())
'''

FACTORY = '''"""Scales numbers."""
class Scaler:
    def __init__(self, factor):
        self.factor = factor

    def scale(self, value: float) -> float:
        """Multiply by the configured factor.

        Args:
            value: the number
        """
        return value * self.factor


def build_tool(config, factor: float = 2.0) -> Scaler:
    return Scaler(config["factor"])
'''


def save(code, name=None, config=None):
    spec = {"code": code, **({"name": name} if name else {}), **({"config": config} if config else {})}
    return custom_tools.save_custom_tool(custom_tools.validate_spec(spec, []))


def zipped(files: dict, root: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in files.items():
            z.writestr(f"{root}/{name}" if root else name, content)
    return buf.getvalue()


class TestAClassIsAToolkit:
    def test_its_public_methods_are_the_tools(self, studio_data):
        spec = save(COUNTER)
        assert spec["kind"] == "class" and spec["name"] == "WordStats"
        assert [t["name"] for t in spec["tools"]] == ["add", "top"]
        add, top = spec["tools"]
        assert add["params"] == [{"name": "text", "type": "string", "description": "the text to count"}]
        assert top["params"][0]["required"] is False
        assert spec["description"] == "Counts and remembers words."
        # Its constructor's typed parameters are the configuration form.
        assert [(f["name"], f["type"], f.get("default")) for f in spec["configuration"]] == [
            ("lowercase", "bool", True), ("minimum", "int", 1)]
        assert "discovery" in spec and spec["discovery"] == "static"

    def test_one_instance_serves_a_whole_run(self, studio_data):
        save(COUNTER, config={"minimum": 3})
        with tool_sessions.scope():
            assert custom_tools.run_custom_tool("add", {"text": "The cat and a hat"}) == {"result": {"added": 4}}
            custom_tools.run_custom_tool("add", {"text": "the end"})
            assert custom_tools.run_custom_tool("top", {"n": 1}) == {"result": ["the"]}
        # Outside a run every call starts over, as it always has.
        assert custom_tools.run_custom_tool("top", {}) == {"result": []}

    def test_the_framework_tool_keeps_its_runs_instance_from_any_thread(self, studio_data):
        spec = save(COUNTER)
        with tool_sessions.scope():
            toolkit = custom_tools.make_toolkit(spec)
        with tool_sessions.scope() as other:
            # Called from a thread that does not carry the run's context --
            # which is how the framework's agents call tools.
            result = {}
            thread = threading.Thread(target=lambda: result.update(
                a=toolkit.get_tool("add")(text="red red blue"), b=toolkit.get_tool("top")(n=1)))
            thread.start()
            thread.join()
            assert result["b"] == {"result": ["red"]}
            assert not other.workers

    def test_a_build_tool_factory_returning_an_object(self, studio_data):
        spec = save(FACTORY, name="scaler", config={"factor": 5.0})
        assert spec["kind"] == "class" and [t["name"] for t in spec["tools"]] == ["scale"]
        assert [f["name"] for f in spec["configuration"]] == ["factor"]
        assert custom_tools.run_custom_tool("scale", {"value": 2.0}) == {"result": 10.0}

    def test_configuration_is_checked_on_save(self, studio_data):
        with pytest.raises(CustomToolError) as raised:
            save(COUNTER, config={"minimum": "three"})
        assert "minimum must be int" in str(raised.value)

    def test_one_of_several_classes_must_be_named(self, studio_data):
        two = COUNTER + '\n\nclass Other:\n    def go(self) -> int:\n        """Go."""\n        return 1\n'
        with pytest.raises(CustomToolError) as raised:
            save(two)
        assert "TOOL_CLASS" in str(raised.value)
        spec = save(two + "\nTOOL_CLASS = Other\n", name="other_kit")
        assert [t["name"] for t in spec["tools"]] == ["go"]

    def test_public_functions_keep_a_module_a_function_toolkit(self, studio_data):
        # Backward compatible: helper classes beside public functions.
        spec = save('"""Kit."""\nclass Helper:\n    def run(self):\n        return 1\n\n'
                    'def ping() -> str:\n    """Ping."""\n    return "pong"\n', name="kit")
        assert "kind" not in spec and [t["name"] for t in spec["tools"]] == ["ping"]
        assert custom_tools.run_custom_tool("ping", {}) == {"result": "pong"}

    def test_undocumented_public_methods_are_refused(self, studio_data):
        with pytest.raises(CustomToolError) as raised:
            save('class T:\n    def go(self, x: int) -> int:\n        return x\n')
        assert "needs a docstring" in str(raised.value)

    def test_a_method_shaped_like_a_metric_is_one(self, studio_data):
        from backend.features.evaluation import evaluation
        save('class Scores:\n    """Scoring."""\n\n    def overlap(self, prediction: str, label: str) -> float:\n'
             '        """Share of label words in the prediction.\n\n        Args:\n'
             '            prediction: the answer\n            label: the expected answer\n        """\n'
             '        want = set(label.split())\n        return len(want & set(prediction.split())) / (len(want) or 1)\n',
             name="scores")
        assert [m["name"] for m in evaluation.custom_metrics()] == ["overlap"]
        assert evaluation.score_one("overlap", "a b", "b c") == {"score": 0.5}

    def test_a_class_tool_can_be_an_input_source(self, studio_data):
        spec = custom_tools.save_custom_tool(custom_tools.validate_spec(
            {"code": COUNTER, "sources": ["top"]}, []))
        assert spec["sources"] == ["top"]
        assert "custom:top" in custom_tools.custom_source_types()


class TestErrorsNameTheLine:
    def test_a_failing_method_names_its_line_and_what_it_printed(self, studio_data):
        save('"""Kit."""\nclass T:\n    def go(self, x: int) -> int:\n        """Go."""\n'
             '        print("about to fail", x)\n        return 1 // x\n', name="kit")
        error = custom_tools.run_custom_tool("go", {"x": 0})["error"]
        assert error.startswith("Tool code line 6: ZeroDivisionError")
        assert "line 6, in go: return 1 // x" in error
        assert "about to fail 0" in error

    def test_a_failing_constructor(self, studio_data):
        save('class T:\n    def __init__(self, path: str = "x"):\n        raise FileNotFoundError(path)\n\n'
             '    def go(self) -> int:\n        """Go."""\n        return 1\n', name="kit")
        error = custom_tools.run_custom_tool("go", {})["error"]
        assert error.startswith("Tool code line 3: FileNotFoundError: x")

    def test_a_failing_import(self, studio_data):
        save('import not_a_real_module_xyz\n\ndef f() -> int:\n    """F."""\n    return 1\n')
        error = custom_tools.run_custom_tool("f", {})["error"]
        assert "line 1" in error and "ModuleNotFoundError" in error


PACKAGE = {
    "mylib/__init__.py": '"""Greetings from a library."""\nfrom .client import Greeter\n\nTOOL_CLASS = Greeter\n',
    "mylib/client.py": ('from .text import shout\n\n\nclass Greeter:\n    """Greets people."""\n\n'
                        '    def __init__(self, greeting: str = "hello"):\n        self.greeting = greeting\n'
                        '        self.count = 0\n\n    def greet(self, who: str) -> str:\n'
                        '        """Greet someone.\n\n        Args:\n            who: the person\n        """\n'
                        '        self.count += 1\n        return shout(f"{self.greeting} {who} #{self.count}")\n\n'
                        '    def fail(self) -> str:\n        """Fail."""\n        return shout(None)\n'),
    "mylib/text.py": "def shout(s):\n    return s.upper()\n",
    "requirements.txt": "requests>=2\n",
}


class TestAnUploadedLibrary:
    def test_a_class_imported_from_a_sibling_module(self, studio_data):
        spec = custom_tools.unpack_toolkit(zipped(PACKAGE), "greeter", None, [], [])
        assert spec["package"]["entry"] == "mylib/__init__.py"
        assert spec["package"]["requirements"] == ["requests>=2"]
        assert spec["kind"] == "class" and spec["discovery"] == "runtime"
        assert [t["name"] for t in spec["tools"]] == ["greet", "fail"]
        assert [f["name"] for f in spec["configuration"]] == ["greeting"]
        with tool_sessions.scope():
            assert custom_tools.run_custom_tool("greet", {"who": "ann"}) == {"result": "HELLO ANN #1"}
            assert custom_tools.run_custom_tool("greet", {"who": "bo"}) == {"result": "HELLO BO #2"}

    def test_an_error_in_a_sibling_module_names_that_file(self, studio_data):
        custom_tools.unpack_toolkit(zipped(PACKAGE), "greeter", None, [], [])
        error = custom_tools.run_custom_tool("fail", {})["error"]
        assert "AttributeError" in error
        assert "mylib/text.py line 2" in error

    def test_checking_an_upload_keeps_nothing(self, studio_data):
        spec = custom_tools.unpack_toolkit(zipped(PACKAGE), "greeter", None, [], [], check=True)
        assert spec["checked"] and [t["name"] for t in spec["tools"]] == ["greet", "fail"]
        assert custom_tools.list_custom_tools() == []
        assert not custom_tools.package_dir("greeter").exists()

    def test_the_entry_can_be_edited_and_is_written_back(self, studio_data):
        custom_tools.unpack_toolkit(zipped(PACKAGE), "greeter", None, [], [])
        edited = PACKAGE["mylib/__init__.py"] + (
            '\n\nclass Polite(Greeter):\n    """Politely."""\n\n'
            '    def thank(self, who: str) -> str:\n        """Thank someone.\n\n'
            '        Args:\n            who: the person\n        """\n'
            '        return "thanks " + who\n\nTOOL_CLASS = Polite\n')
        spec = custom_tools.save_package_entry("greeter", {"code": edited, "config": {"greeting": "hi"}}, [])
        assert (custom_tools.package_dir("greeter") / "mylib/__init__.py").read_text() == edited
        assert spec["package"]["entry"] == "mylib/__init__.py"
        assert [t["name"] for t in spec["tools"]] == ["greet", "fail", "thank"]
        assert custom_tools.run_custom_tool("greet", {"who": "x"}) == {"result": "HI X #1"}

    def test_loose_files_are_a_folder_too(self, studio_data):
        blob = custom_tools.zip_files([
            ("proj/tools.py", b'"""Kit."""\nfrom helpers import twice\n\n'
                              b'def double(s: str) -> str:\n    """Double.\n\n    Args:\n        s: text\n    """\n'
                              b'    return twice(s)\n'),
            ("proj/helpers.py", b"def twice(s):\n    return s * 2\n")])
        spec = custom_tools.unpack_toolkit(blob, None, None, [], [])
        assert spec["name"] == "proj"
        assert custom_tools.run_custom_tool("double", {"s": "ab"}) == {"result": "abab"}


class TestTheEndpoints:
    @pytest.fixture
    def client(self, studio_data):
        from fastapi.testclient import TestClient
        from backend.api import app as studio_app
        return TestClient(studio_app.app)

    def test_interface_lists_tools_without_running(self, client):
        res = client.post("/api/tools/custom/interface",
                          json={"code": COUNTER + '\nraise RuntimeError("never run")\n'})
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["kind"] == "class" and body["class"] == "WordStats"
        assert [t["name"] for t in body["tools"]] == ["add", "top"]
        assert [f["name"] for f in body["configuration"]["inputs"]] == ["lowercase", "minimum"]

    def test_interface_reads_an_imported_class_only_when_asked(self, client):
        code = '"""Kit."""\nfrom collections import OrderedDict\n\nclass _Base:\n    pass\n\nTOOL_CLASS = OrderedDict\n'
        res = client.post("/api/tools/custom/interface", json={"code": code})
        assert res.status_code == 200 and res.json()["needs_discovery"] is True
        assert res.json()["tools"] == []
        # An installed library's methods are not the author's tools.
        res = client.post("/api/tools/custom/interface", json={"code": code, "execute": True})
        assert res.status_code == 422 and "no public methods" in res.json()["detail"]

    def test_interface_reports_a_syntax_error_line(self, client):
        res = client.post("/api/tools/custom/interface", json={"code": "class T:\n    def x(self\n"})
        assert res.status_code == 422 and "line" in res.json()["detail"]

    def test_preview_calls_one_instance_in_turn_and_saves_nothing(self, client):
        res = client.post("/api/tools/custom/preview", json={
            "code": COUNTER.replace('        return {"added": len(words)}',
                                    '        print("adding", len(words))\n        return {"added": len(words)}'),
            "calls": [{"tool": "add", "args": {"text": "a b a"}}, {"tool": "top", "args": {"n": 1}}]})
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["result"] == ["a"]
        assert body["results"][0]["logs"].strip() == "adding 3"
        assert custom_tools.list_custom_tools() == []

    def test_preview_error_names_the_line(self, client):
        res = client.post("/api/tools/custom/preview", json={
            "code": 'class T:\n    def go(self, x: int) -> int:\n        """Go."""\n        return 1 // x\n',
            "tool": "go", "args": {"x": 0}})
        assert res.status_code == 422 and res.json()["detail"].startswith("Tool code line 4")

    def test_editing_a_folder_toolkit_needs_edit_entry(self, client):
        custom_tools.unpack_toolkit(zipped(PACKAGE), "greeter", None, [], [])
        code = PACKAGE["mylib/__init__.py"].replace("Greetings", "Hellos")
        refused = client.post("/api/tools/custom", json={"name": "greeter", "code": code})
        assert refused.status_code == 422 and "edit_entry" in refused.json()["detail"]
        saved = client.post("/api/tools/custom", json={"name": "greeter", "code": code, "edit_entry": True})
        assert saved.status_code == 200, saved.text
        assert saved.json()["description"].startswith("Hellos")

    def test_upload_of_loose_files_with_check(self, client):
        res = client.post("/api/tools/custom/upload", data={"check": "true"}, files=[
            ("files", ("lib/tools.py", COUNTER.encode(), "text/x-python")),
            ("files", ("lib/README.md", b"docs", "text/markdown"))])
        assert res.status_code == 200, res.text
        assert res.json()["checked"] and res.json()["name"] == "lib"
        assert client.get("/api/tools/custom").json()["tools"] == []


class TestInAWorkflow:
    def test_two_nodes_share_one_instance_per_run(self, studio_data):
        from backend.api import runner
        save(COUNTER)
        graph = {"id": "class-tool-run", "name": "Class tool", "goal": "Count", "flow_version": 2,
                 "tasks": [{"name": "count", "kind": "tool", "tool": "add",
                            "inputs": [{"name": "text", "type": "str", "required": True}],
                            "outputs": [{"name": "added", "type": "int"}]},
                           {"name": "best", "kind": "tool", "tool": "top",
                            "inputs": [{"name": "n", "type": "int", "required": False}],
                            "outputs": [{"name": "words", "type": "list"}]}],
                 "edges": [{"source": "count", "target": "best", "control_only": True}]}
        for _ in range(2):
            rid = runner.start_run(graph, {"text": "go go stop"}, background=False)
            run = runner.get_run(rid)
            assert run["status"] == "success", run.get("error")
            # The second run starts from a fresh instance.
            assert run["node_outputs"]["best"] == {"words": ["go", "stop"]}

    def test_an_exported_project_runs_the_class_tool(self, studio_data, tmp_path):
        import os
        import subprocess
        import sys
        from pathlib import Path

        from backend.api import export_api, graphs
        save(COUNTER, config={"lowercase": False})
        custom_tools.unpack_toolkit(zipped(PACKAGE), "greeter", None, [], [])
        graph = {"id": "expclass", "name": "expclass", "goal": "g", "flow_version": 2,
                 "tasks": [{"name": "A", "kind": "tool", "tool": "add", "enabled": True,
                            "inputs": [{"name": "text", "type": "str"}],
                            "outputs": [{"name": "added", "type": "int"}]},
                           {"name": "G", "kind": "tool", "tool": "greet", "enabled": True,
                            "inputs": [{"name": "who", "type": "str"}],
                            "outputs": [{"name": "text", "type": "str"}]}],
                 "edges": []}
        graphs.validate_graph(graph)
        files, binary = export_api.project_files(graph, include_vendor=False)
        assert "vendor/tool_runtime.py" in files
        out = tmp_path / "project"
        for rel, content in files.items():
            (out / rel).parent.mkdir(parents=True, exist_ok=True)
            (out / rel).write_text(content, encoding="utf-8")
        for rel, blob in binary.items():
            (out / rel).parent.mkdir(parents=True, exist_ok=True)
            (out / rel).write_bytes(blob)
        (out / "driver.py").write_text('''
import json, sys
sys.path.insert(0, ".")
import workflow
nodes = {t["name"]: t for t in workflow.TOOL_NODES}
print(json.dumps([workflow.call_tool_node(nodes["A"], {"text": "Go go"}),
                  workflow.call_tool_node(nodes["G"], {"who": "x"}),
                  workflow.call_tool_node(nodes["G"], {"who": "y"})]))
''', encoding="utf-8")
        repo = Path(__file__).resolve().parents[2]
        proc = subprocess.run([sys.executable, "driver.py"], cwd=out, capture_output=True, text=True,
                              timeout=300, env={**os.environ, "PYTHONPATH": str(repo / "backend")})
        assert proc.returncode == 0, proc.stderr[-3000:]
        assert json.loads(proc.stdout.strip().splitlines()[-1]) == [
            # One output: the whole return value, exactly as for a function tool.
            {"added": {"added": 2}}, {"text": "HELLO X #1"}, {"text": "HELLO Y #2"}]


class TestBackwardCompatible:
    def test_the_oldest_single_run_shape_still_runs(self, studio_data):
        custom_tools.TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        (custom_tools.TOOLS_DIR / "old.json").write_text(json.dumps(
            {"name": "old", "description": "Old.", "params": [{"name": "text", "type": "string"}],
             "code": "def run(text):\n    return text[::-1]\n"}))
        assert custom_tools.run_custom_tool("old", {"text": "ab"}) == {"result": "ba"}

    def test_a_timeout_is_reported_and_the_run_goes_on(self, studio_data, monkeypatch):
        monkeypatch.setattr(custom_tools, "EXEC_TIMEOUT", 2)
        save('class T:\n    """Slow and fast."""\n\n    def slow(self) -> int:\n        """Slow."""\n        import time\n'
             '        time.sleep(30)\n        return 1\n\n    def fast(self) -> int:\n        """Fast."""\n        return 2\n',
             name="kit")
        with tool_sessions.scope():
            assert "timed out" in custom_tools.run_custom_tool("slow", {})["error"]
            assert custom_tools.run_custom_tool("fast", {}) == {"result": 2}

    def test_an_async_run_loop_reaches_the_scope(self, studio_data):
        save(COUNTER)

        async def twice():
            custom_tools.run_custom_tool("add", {"text": "x y"})
            return await asyncio.to_thread(custom_tools.run_custom_tool, "top", {})
        with tool_sessions.scope():
            assert asyncio.run(twice()) == {"result": ["x", "y"]}
