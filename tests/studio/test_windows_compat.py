"""Studio on Windows, with its data in a OneDrive folder.

Paths are compared by component (backslashes, case-insensitive names), worker
processes are ended without POSIX-only calls, text files are read and written
as UTF-8 rather than the ANSI code page, non-ASCII download names do not turn
into a 500, ids from a URL cannot leave their data folder, and a replace that
a sync client blocks for a moment is tried again.
"""
import ast
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.features import persistence, proc
from backend.features.http_util import content_disposition


# ---------------------------------------------------------------------------
# Containment
# ---------------------------------------------------------------------------

class TestContainment:
    def test_windows_paths_compare_by_component_and_ignore_case(self):
        root = PureWindowsPath(r"C:\Users\me\OneDrive\studio-data\workspace\g")
        assert persistence.contains(root, root / "files" / "a.csv")
        assert persistence.contains(root, PureWindowsPath(r"c:\users\ME\onedrive\Studio-Data\workspace\G\x"))
        assert not persistence.contains(root, PureWindowsPath(r"C:\Users\me\OneDrive\studio-data\workspace\g-evil\x"))
        assert not persistence.contains(root, PureWindowsPath(r"D:\elsewhere"))

    def test_a_sibling_with_the_same_prefix_is_outside(self):
        root = PurePosixPath("/data/ws")
        assert not persistence.contains(root, PurePosixPath("/data/ws-evil/x"))
        assert persistence.contains(root, PurePosixPath("/data/ws/x"))

    def test_the_root_itself_only_when_allowed(self):
        root = PureWindowsPath(r"C:\data")
        assert not persistence.contains(root, root)
        assert persistence.contains(root, root, allow_root=True)

    def test_is_within_resolves_dot_dot(self, tmp_path):
        assert persistence.is_within(tmp_path / "a" / ".." / "b", tmp_path)
        assert not persistence.is_within(tmp_path / ".." / "x", tmp_path)

    def test_forgetting_one_graphs_tables_keeps_the_others(self, monkeypatch):
        from backend.api import table_store
        monkeypatch.setattr(table_store, "_ready", {
            str(table_store.TABLES_DIR / "g" / "a.db"),
            str(table_store.TABLES_DIR / "g-2" / "a.db")})
        table_store.forget("g")
        assert table_store._ready == {str(table_store.TABLES_DIR / "g-2" / "a.db")}


# ---------------------------------------------------------------------------
# Worker processes
# ---------------------------------------------------------------------------

class _Process:
    pid = 4242

    def __init__(self):
        self.killed = False

    def poll(self):
        return None

    def kill(self):
        self.killed = True


class TestKillTree:
    def test_posix_starts_a_session_and_kills_the_group(self, monkeypatch):
        monkeypatch.setattr(proc.sys, "platform", "linux")
        calls = []
        monkeypatch.setattr(proc.os, "killpg", lambda pid, sig: calls.append((pid, sig)), raising=False)
        assert proc.popen_group_kwargs() == {"start_new_session": True}
        proc.kill_tree(_Process())
        assert calls == [(4242, proc.signal.SIGKILL)]

    def test_windows_uses_a_process_group_and_taskkill(self, monkeypatch):
        monkeypatch.setattr(proc.sys, "platform", "win32")
        ran = []
        monkeypatch.setattr(proc.subprocess, "run", lambda args, **kw: ran.append(args))

        def no_killpg(*args):
            raise AssertionError("os.killpg does not exist on Windows")
        monkeypatch.setattr(proc.os, "killpg", no_killpg, raising=False)
        assert proc.popen_group_kwargs() == {"creationflags": proc.CREATE_NEW_PROCESS_GROUP}
        process = _Process()
        proc.kill_tree(process)
        assert ran == [["taskkill", "/T", "/F", "/PID", "4242"]]
        assert process.killed        # still alive after taskkill: killed directly

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
    def test_a_real_worker_and_its_child_are_ended(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import subprocess, sys, time; "
             "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); time.sleep(60)"],
            **proc.popen_group_kwargs())
        proc.kill_tree(process)
        assert process.wait(timeout=10) != 0

    def test_workers_run_python_in_utf8_mode(self, monkeypatch):
        from backend.api import chat_control
        monkeypatch.delenv("PYTHONUTF8", raising=False)
        assert chat_control.worker_env()["PYTHONUTF8"] == "1"

    def test_no_feature_module_calls_killpg_directly(self):
        backend = Path(__file__).resolve().parents[2] / "backend"
        offenders = [str(p) for folder in ("features", "api") for p in (backend / folder).rglob("*.py")
                     if p.name != "proc.py" and ("os.killpg" in p.read_text(encoding="utf-8")
                                                 or "start_new_session=True" in p.read_text(encoding="utf-8"))]
        assert offenders == []


# ---------------------------------------------------------------------------
# Text encoding: a regression lint over the backend source
# ---------------------------------------------------------------------------

_SKIPPED = {"evoagentx", "node_modules", "legacy_studio_pycache", "__pycache__", "llm"}


def _mode(call, position):
    for keyword in call.keywords:
        if keyword.arg == "mode":
            return keyword.value
    return call.args[position] if len(call.args) > position else None


def _missing_encoding(path):
    """Text-mode open / read_text / write_text calls in `path` without an
    explicit encoding (Windows would use the ANSI code page)."""
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        keywords = {k.arg for k in node.keywords}
        if "encoding" in keywords or None in keywords:     # **kwargs may carry it
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
        if name in ("read_text", "write_text") and isinstance(func, ast.Attribute):
            found.append((node.lineno, name))
        elif name in ("open", "fdopen"):
            module_call = isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) \
                and func.value.id in ("io", "os", "codecs", "builtins")
            mode = _mode(node, 1 if isinstance(func, ast.Name) or name == "fdopen" or module_call else 0)
            if mode is None or (isinstance(mode, ast.Constant) and isinstance(mode.value, str)
                                and "b" not in mode.value):
                found.append((node.lineno, name))
    return found


def test_backend_text_io_names_its_encoding():
    backend = Path(__file__).resolve().parents[2] / "backend"
    missing = []
    for path in sorted(backend.rglob("*.py")):
        if _SKIPPED & set(path.relative_to(backend).parts):
            continue
        missing += [f"{path.relative_to(backend)}:{line}: {name}" for line, name in _missing_encoding(path)]
    assert missing == [], "add encoding='utf-8':\n" + "\n".join(missing)


def test_the_lint_catches_what_it_should(tmp_path):
    probe = tmp_path / "probe.py"
    probe.write_text("open(p)\nopen(p, 'w')\nopen(p, 'rb')\nopen(p, encoding='utf-8')\n"
                     "p.read_text()\np.write_text(s, encoding='utf-8')\np.open('a')\np.open('wb')\n",
                     encoding="utf-8")
    assert [line for line, _ in _missing_encoding(probe)] == [1, 2, 5, 7]


# ---------------------------------------------------------------------------
# Download names
# ---------------------------------------------------------------------------

class TestContentDisposition:
    def test_non_ascii_names_get_an_rfc_6266_header(self):
        value = content_disposition("数据.csv")
        value.encode("latin-1")                          # a valid header value
        assert value == "attachment; filename=\"download.csv\"; filename*=UTF-8''%E6%95%B0%E6%8D%AE.csv"

    def test_ascii_names_are_unchanged(self):
        assert content_disposition("run.zip") == 'attachment; filename="run.zip"'

    def test_quotes_and_line_breaks_cannot_break_the_header(self):
        value = content_disposition('a"b\r\nc.txt')
        assert "\r" not in value and "\n" not in value
        assert value.startswith('attachment; filename="a_bc.txt"')

    def test_downloading_a_chinese_named_workspace_file(self, studio_data, monkeypatch):
        from backend.api import graphs, workspace, workspace_api
        monkeypatch.setattr(workspace, "WORKSPACE_DIR", studio_data / "workspace")
        graph = graphs.create_graph("Data probe", "")
        target = workspace.workspace_root(graph["id"]) / "files" / "数据.csv"
        target.parent.mkdir(parents=True)
        target.write_text("a,b\n1,2\n", encoding="utf-8")
        app = FastAPI()
        app.include_router(workspace_api.router)

        response = TestClient(app).get(f"/api/graphs/{graph['id']}/workspace/download",
                                       params={"path": "files/数据.csv"})

        assert response.status_code == 200
        assert response.content == b"a,b\n1,2\n"
        assert "filename*=UTF-8''%E6%95%B0%E6%8D%AE.csv" in response.headers["content-disposition"]


# ---------------------------------------------------------------------------
# Ids from a URL
# ---------------------------------------------------------------------------

UNSAFE = ["", "..", "../x", "..\\x", "a/b", "a\\b", "C:x", "x.", ".hidden", "a..b", None, 7]


class TestIds:
    @pytest.mark.parametrize("value", UNSAFE)
    def test_unsafe_ids_are_rejected(self, value):
        assert not persistence.is_safe_id(value)
        with pytest.raises(persistence.InvalidId):
            persistence.safe_id(value)

    @pytest.mark.parametrize("value", ["probe", "my-graph_2", "0a1b2c3d4e5f", "v1.2"])
    def test_ordinary_ids_pass(self, value):
        assert persistence.safe_id(value) == value

    @pytest.mark.parametrize("value", ["../x", "..\\x", ""])
    def test_stores_treat_an_unsafe_id_as_missing(self, studio_data, value):
        from backend.api import batch, graphs, review, runner, scheduler
        assert graphs.load_graph(value) is None
        assert graphs.graph_exists(value) is False
        assert graphs.delete_graph(value) is False
        assert batch.get_batch(value) is None
        assert runner._read_run(value) is None
        assert review.get_review(value) is None
        assert scheduler.load(value) is None
        assert scheduler.clear(value) is False
        evolve_api = pytest.importorskip("backend.api.evolve_api")
        assert evolve_api.get_task(value) is None

    def test_the_graph_route_answers_404(self, studio_data):
        from backend.api import app as studio_app
        client = TestClient(studio_app.app)
        assert client.get("/api/graphs/..%5Csecret").status_code == 404
        assert client.get("/api/graphs/..").status_code in (404, 405)


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------

@pytest.fixture
def ws(studio_data, monkeypatch):
    from backend.api import workspace
    monkeypatch.setattr(workspace, "WORKSPACE_DIR", studio_data / "workspace")
    workspace.workspace_root("probe").mkdir(parents=True)
    (workspace.workspace_root("probe") / "keep.txt").write_text("x", encoding="utf-8")
    return workspace


class TestWorkspace:
    @pytest.mark.parametrize("relpath", ["", ".", "files/..", "./"])
    def test_deleting_the_workspace_itself_is_refused(self, ws, relpath):
        with pytest.raises(ws.WorkspaceError):
            ws.delete_file("probe", relpath, recursive=True)
        assert (ws.workspace_root("probe") / "keep.txt").is_file()

    def test_traversal_is_refused(self, ws):
        with pytest.raises(ws.WorkspaceError):
            ws.read_file("probe", "../other/x.txt")

    def test_upload_copies_where_hard_links_are_unavailable(self, ws, monkeypatch):
        import io

        def no_links(source, target):
            raise PermissionError(1, "Operation not permitted")
        monkeypatch.setattr(os, "link", no_links)

        result = ws.upload_file("probe", "files/数据.csv", io.BytesIO(b"a,b\n"))

        assert (ws.workspace_root("probe") / "files" / "数据.csv").read_bytes() == b"a,b\n"
        assert result["size"] == 4
        with pytest.raises(ws.WorkspaceError, match="already exists"):
            ws.upload_file("probe", "files/数据.csv", io.BytesIO(b"other"))
        assert (ws.workspace_root("probe") / "files" / "数据.csv").read_bytes() == b"a,b\n"
        leftovers = [p.name for p in (ws.workspace_root("probe") / "files").iterdir()]
        assert leftovers == ["数据.csv"]


# ---------------------------------------------------------------------------
# Replacing a file a sync client holds for a moment
# ---------------------------------------------------------------------------

class TestReplaceRetry:
    def test_a_transient_permission_error_is_retried(self, tmp_path, monkeypatch):
        real = os.replace
        failures = [PermissionError(13, "in use"), PermissionError(13, "in use")]

        def flaky(source, destination):
            if failures:
                raise failures.pop(0)
            real(source, destination)
        monkeypatch.setattr(persistence.os, "replace", flaky)
        monkeypatch.setattr(persistence.time, "sleep", lambda seconds: None)

        persistence.write_json(tmp_path / "state.json", {"name": "数据"})

        assert (tmp_path / "state.json").read_text(encoding="utf-8").count("数据") == 1
        assert not list(tmp_path.glob("*.tmp"))

    def test_a_lasting_one_is_raised(self, tmp_path, monkeypatch):
        def locked(source, destination):
            raise PermissionError(13, "in use")
        monkeypatch.setattr(persistence.os, "replace", locked)
        monkeypatch.setattr(persistence.time, "sleep", lambda seconds: None)

        with pytest.raises(PermissionError):
            persistence.write_json(tmp_path / "state.json", {})
        assert not list(tmp_path.glob("*.tmp"))
