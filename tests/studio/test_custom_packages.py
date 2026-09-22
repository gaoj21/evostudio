"""A toolkit can be a folder: a library, a project, anything with an API.

A pasted module is the small case. The principle from the start was that a
tool is a documented calling interface, wherever it lives — so a zip of a
folder is a toolkit too: its entry module's public functions are the tools,
and the rest of the folder (sibling modules, data, a requirements list)
comes along and is there when they run.
"""

import io
import json
import zipfile

import pytest

from backend.api import custom_tools


def zipped(files: dict, root: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in files.items():
            z.writestr(f"{root}/{name}" if root else name, content)
    return buf.getvalue()


PROJECT = {
    "tools.py": '''"""Ticker helpers, built on a sibling module."""
from helpers import normalise

def clean(symbol: str) -> dict:
    """Normalise a ticker.

    Args:
        symbol: raw ticker text
    """
    return {"symbol": normalise(symbol), "from_file": open("note.txt").read().strip()}
''',
    "helpers.py": "def normalise(s):\n    return s.strip().upper()\n",
    "note.txt": "hello from the folder\n",
    "requirements.txt": "# comment\nrequests>=2\n",
}


def upload(files=PROJECT, name="tickerkit", entry=None, root=None, sources=()):
    return custom_tools.unpack_toolkit(zipped(files, root), name, entry, list(sources), [])


class TestAFolderIsAToolkit:
    def test_the_entry_modules_functions_are_the_tools(self, studio_data):
        spec = upload()
        assert [t["name"] for t in spec["tools"]] == ["clean"]
        assert spec["package"]["entry"] == "tools.py"
        assert spec["package"]["files"] == 4
        assert spec["package"]["requirements"] == ["requests>=2"]
        assert (custom_tools.package_dir("tickerkit") / "helpers.py").is_file()

    def test_it_runs_from_inside_its_folder(self, studio_data):
        # Sibling import and a file beside it: both only work if the tool
        # runs from the folder, on the path — which is the whole point.
        upload()
        out = custom_tools.run_custom_tool("clean", {"symbol": " aapl "})
        assert out == {"result": {"symbol": "AAPL", "from_file": "hello from the folder"}}

    def test_a_zip_of_a_folder_loses_its_wrapping_folder(self, studio_data):
        spec = upload(root="my-project", name=None)
        assert spec["name"] == "my_project"                 # named after the folder, as an identifier
        assert (custom_tools.package_dir("my_project") / "tools.py").is_file()
        assert custom_tools.run_custom_tool("clean", {"symbol": "x"})["result"]["symbol"] == "X"

    def test_the_entry_can_be_named(self, studio_data):
        spec = upload({"api/service.py": PROJECT["tools.py"], "helpers.py": PROJECT["helpers.py"],
                       "note.txt": "n"}, entry="api/service.py")
        assert spec["package"]["entry"] == "api/service.py"

    def test_an_ambiguous_folder_asks_for_the_entry(self, studio_data):
        with pytest.raises(custom_tools.CustomToolError) as err:
            upload({"a.py": "def f() -> int:\n    'F.'\n    return 1\n",
                    "b.py": "def g() -> int:\n    'G.'\n    return 2\n"})
        assert "entry" in str(err.value).lower()

    def test_a_package_with_an_init_is_its_own_entry(self, studio_data):
        spec = upload({"pkg/__init__.py": PROJECT["tools.py"].replace("from helpers", "from .helpers"),
                       "pkg/helpers.py": PROJECT["helpers.py"], "pkg/note.txt": "n"})
        assert spec["package"]["entry"] == "pkg/__init__.py"

    def test_a_member_that_would_escape_is_refused(self, studio_data):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("../escape.py", "def f() -> int:\n    'F.'\n    return 1\n")
        with pytest.raises(custom_tools.CustomToolError):
            custom_tools.unpack_toolkit(buf.getvalue(), "bad", None, [], [])

    def test_not_a_zip_says_so(self, studio_data):
        with pytest.raises(custom_tools.CustomToolError) as err:
            custom_tools.unpack_toolkit(b"just bytes", "bad", None, [], [])
        assert "zip" in str(err.value).lower()

    def test_deleting_the_toolkit_removes_its_folder(self, studio_data):
        upload()
        assert custom_tools.delete_custom_tool("tickerkit")
        assert not custom_tools.package_dir("tickerkit").exists()

    def test_marking_a_source_keeps_the_package(self, studio_data):
        # Re-saving through the ordinary spec route (what the source toggle
        # does) must not drop the folder or replace the code.
        upload()
        again = custom_tools.validate_spec({"name": "tickerkit", "code": "def clean(symbol: str) -> dict:\n    'C.'\n    return {}\n",
                                            "sources": ["clean"]}, [])
        saved = custom_tools.save_custom_tool(again)
        assert saved["package"]["entry"] == "tools.py"
        assert "from helpers import normalise" in saved["code"]
        assert saved["sources"] == ["clean"]
        assert custom_tools.run_custom_tool("clean", {"symbol": "q"})["result"]["symbol"] == "Q"


class TestTheEndpoint:
    @pytest.fixture
    def client(self, studio_data):
        from fastapi.testclient import TestClient
        from backend.api import app as studio_app
        return TestClient(studio_app.app)

    def test_upload_and_list(self, client):
        res = client.post("/api/tools/custom/upload",
                          files={"file": ("proj.zip", zipped(PROJECT), "application/zip")},
                          data={"name": "tickerkit", "sources": json.dumps(["clean"])})
        assert res.status_code == 200, res.text
        listed = client.get("/api/tools/custom").json()["tools"]
        assert listed[0]["package"]["requirements"] == ["requests>=2"]
        assert listed[0]["sources"] == ["clean"]

    def test_a_bad_archive_is_a_422(self, client):
        res = client.post("/api/tools/custom/upload",
                          files={"file": ("x.zip", b"nope", "application/zip")})
        assert res.status_code == 422

    def test_installing_requirements_is_an_explicit_step(self, client, monkeypatch):
        import subprocess
        client.post("/api/tools/custom/upload",
                    files={"file": ("proj.zip", zipped(PROJECT), "application/zip")},
                    data={"name": "tickerkit"})
        calls = []
        monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd) or
                            type("P", (), {"returncode": 0, "stdout": "ok", "stderr": ""})())
        res = client.post("/api/tools/custom/tickerkit/install")
        assert res.status_code == 200, res.text
        assert calls and "install" in calls[0] and "-r" in calls[0]


class TestItTravelsWithAnExport:
    def test_the_folder_is_bundled(self, studio_data):
        from conftest import make_graph, make_task
        from backend.api import export_api
        upload()
        t = make_task("fix", inputs=["symbol"], outputs=["out"]); t["kind"] = "tool"; t["tool"] = "clean"
        g = make_graph([t]); g["id"] = "g1"; g["name"] = "G"; g["edges"] = []
        files, _ = export_api.project_files(g, include_vendor=False)
        assert "tools/tickerkit/tools.py" in files
        assert "tools/tickerkit/helpers.py" in files
        assert json.loads(files["tools/tickerkit.json"])["package"]["entry"] == "tools.py"
