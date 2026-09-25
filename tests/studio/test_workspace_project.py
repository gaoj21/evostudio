"""The workspace is the workflow's project, not a folder of leftovers.

It holds what `Export` hands to someone else — the generated code, the graph,
the tools and skills it uses — compiled from the canvas whenever the workflow
is saved, alongside everything its runs produce. The vendored framework is the
one thing that lives only in the export: 3.7 MB of it, and it matters only
once the project leaves this machine.
"""

import json

import pytest

from conftest import make_graph, make_task


@pytest.fixture
def store(studio_data, monkeypatch):
    from backend.api import graphs
    from backend.api import workspace
    monkeypatch.setattr(workspace, "WORKSPACE_DIR", studio_data / "workspace")
    return graphs, workspace


def graph_with(name="Probe", **extra):
    return {
        **make_graph([make_task("first", inputs=["country"], outputs=["capital"]),
                      make_task("second", inputs=["capital"], outputs=["blurb"])],
                     edges=[("first", "second")]),
        "id": "probe", "name": name, **extra,
    }


def listing(workspace, graph_id):
    root = workspace.workspace_root(graph_id)
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


class TestTheProjectLivesThere:
    def test_saving_compiles_the_canvas_into_the_workspace(self, store):
        graphs, workspace = store
        workspace.write_project(graph_with())

        files = listing(workspace, "probe")
        assert "workflow.py" in files      # the runnable code
        assert "run.py" in files           # how to run it
        assert "graph.json" in files       # what the canvas holds
        assert "README.md" in files
        assert "requirements.txt" in files
        assert "data/sample_input.json" in files

    def test_the_code_reflects_the_canvas(self, store):
        graphs, workspace = store
        workspace.write_project(graph_with())
        source = (workspace.workspace_root("probe") / "workflow.py").read_text()

        assert "'name': 'first'" in source and "'name': 'second'" in source

    def test_re_saving_follows_the_canvas(self, store):
        graphs, workspace = store
        workspace.write_project(graph_with())
        smaller = {**graph_with(),
                   "tasks": [make_task("only", inputs=["country"], outputs=["out"])],
                   "edges": []}
        workspace.write_project(smaller)

        source = (workspace.workspace_root("probe") / "workflow.py").read_text()
        # Otherwise the project on disk keeps describing a workflow that is no
        # longer on the canvas.
        assert "'name': 'only'" in source
        assert "'name': 'second'" not in source

    def test_a_tool_the_workflow_stopped_using_is_cleared_out(self, store, monkeypatch):
        from backend.api import custom_tools
        from backend.api import tools_registry
        graphs, workspace = store
        monkeypatch.setattr(custom_tools, "TOOLS_DIR", studio := workspace.WORKSPACE_DIR.parent / "tools")
        custom_tools.save_custom_tool(custom_tools.validate_spec(
            {"code": 'def shout(text: str) -> dict:\n    """Shout."""\n    return {}\n'},
            tools_registry.builtin_names()))

        using = graph_with()
        using["tasks"][0]["tool_names"] = ["shout"]
        workspace.write_project(using)
        assert "tools/shout.py" in listing(workspace, "probe")

        workspace.write_project(graph_with())
        assert "tools/shout.py" not in listing(workspace, "probe")

    def test_run_artifacts_are_never_touched_by_a_recompile(self, store):
        graphs, workspace = store
        run_file = workspace.workspace_root("probe") / "runs" / "r1" / "output.json"
        run_file.parent.mkdir(parents=True, exist_ok=True)
        run_file.write_text('{"kept": true}', encoding="utf-8")

        workspace.write_project(graph_with())

        # Recompiling the project must not throw away what its runs produced.
        assert json.loads(run_file.read_text())["kept"] is True

    def test_a_users_own_files_are_never_touched(self, store):
        graphs, workspace = store
        mine = workspace.files_dir("probe") / "notes.txt"
        mine.parent.mkdir(parents=True, exist_ok=True)
        mine.write_text("mine", encoding="utf-8")

        workspace.write_project(graph_with())
        assert mine.read_text(encoding="utf-8") == "mine"

    def test_the_framework_and_the_layers_are_there_too(self, store):
        graphs, workspace = store
        workspace.write_project(graph_with())

        # What you can browse here has to be what you could deploy; a project
        # missing the framework is not that.
        files = listing(workspace, "probe")
        assert any(f.startswith("vendor/evoagentx/") for f in files)
        assert any(f.startswith("vendor/llm/") for f in files)
        assert any(f.startswith("vendor/memory/") for f in files)

    def test_the_framework_is_written_once_and_left_alone(self, store):
        graphs, workspace = store
        workspace.write_project(graph_with())
        marker = workspace.workspace_root("probe") / "vendor" / "evoagentx" / "__init__.py"
        before = marker.stat().st_mtime_ns

        workspace.write_project(graph_with(name="Renamed"))

        # 3.8 MB of identical bytes rewritten on every canvas save would be
        # churn for nothing.
        assert marker.stat().st_mtime_ns == before

    def test_it_is_rewritten_when_the_source_changes(self, store, monkeypatch):
        from backend.api import export_api
        graphs, workspace = store
        workspace.write_project(graph_with())
        marker = workspace.workspace_root("probe") / "vendor" / "evoagentx" / "__init__.py"
        marker.unlink()

        monkeypatch.setattr(export_api, "vendor_fingerprint", lambda: "something-else")
        workspace.write_project(graph_with())
        assert marker.is_file()

    def test_the_stamp_is_not_shown_as_part_of_the_project(self, store):
        graphs, workspace = store
        workspace.write_project(graph_with())

        paths = [e["path"] for e in workspace.tree("probe")]
        assert workspace.VENDOR_STAMP not in paths
        assert not any("__pycache__" in p for p in paths)


class TestItMatchesTheExport:
    def test_the_same_files_with_the_same_contents(self, store):
        from backend.api import export_api
        graphs, workspace = store
        graph = graph_with()
        workspace.write_project(graph)
        files, _vendor = export_api.project_files(graph)

        on_disk = [f for f in listing(workspace, "probe")
                   if not f.startswith("vendor/")
                   and f not in (workspace.VENDOR_STAMP, workspace.GENERATED_MANIFEST)]
        files = {k: v for k, v in files.items() if not k.startswith("vendor/")}
        assert set(on_disk) == set(files)
        root = workspace.workspace_root("probe")
        # manifest.json carries the time it was written, so it differs by design.
        for rel in on_disk:
            if rel != "manifest.json":
                assert (root / rel).read_text(encoding="utf-8") == files[rel], rel


class TestEveryNodeHasItsOwnLog:
    """A folder per run or batch, named by when it started, and inside it one
    file per node that every record of that session appends to.

    Open `<started>/nodes/decide.jsonl` and read every decision of that batch,
    in order, each with the inputs it was handed and the run it belonged to.
    The run-level summary is appended to `runs.jsonl` beside `nodes/`.
    """

    STARTED = "2026-09-01T10:20:30"          # local, naive: folder 20260901-102030

    @classmethod
    def state(cls, nodes, **extra):
        return {"run_id": "r1", "status": "failed", "created_at": cls.STARTED,
                "inputs": {"country": "Peru"}, "nodes": nodes,
                "_node_io": {n["name"]: {"inputs": {"country": "Peru"}, "output": n.get("output")}
                             for n in nodes}, **extra}

    @staticmethod
    def lines(workspace, rel, folder="20260901-102030"):
        path = workspace.workspace_root("probe") / "runs" / folder / rel
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l]

    def test_each_node_gets_a_line_in_its_own_file(self, store):
        graphs, workspace = store
        workspace.write_run_artifacts("probe", self.state([
            {"name": "first", "status": "completed", "output": {"capital": "Lima"}},
            {"name": "second", "status": "failed", "output": None},
        ]))

        first = self.lines(workspace, "nodes/first.jsonl")
        assert len(first) == 1
        assert first[0]["inputs"] == {"country": "Peru"}
        assert first[0]["output"] == {"capital": "Lima"}
        assert first[0]["run_id"] == "r1"
        assert first[0]["session"] == "20260901-102030"
        assert self.lines(workspace, "nodes/second.jsonl")[0]["status"] == "failed"
        assert self.lines(workspace, "runs.jsonl")[0]["status"] == "failed"
        assert not (workspace.workspace_root("probe") / "runs" / "r1").exists()

    def test_the_records_of_one_batch_share_a_folder_and_append(self, store):
        # Two records of the same batch: same session start, same folder,
        # each appending its line to the node's file.
        graphs, workspace = store
        for run_id, d in (("r1", 1), ("r2", 2)):
            workspace.write_run_artifacts("probe", {
                **self.state([{"name": "decide", "status": "completed", "output": {"d": d}}]),
                "run_id": run_id, "batch_id": "batch-9", "created_at": "2026-09-01T10:20:3" + str(d),
                "session_started_at": "2026-09-01T10:19:00"})

        decide = self.lines(workspace, "nodes/decide.jsonl", folder="20260901-101900")
        assert [(l["run_id"], l["output"]["d"]) for l in decide] == [("r1", 1), ("r2", 2)]
        assert decide[1]["batch_id"] == "batch-9"
        assert [l["run_id"] for l in self.lines(workspace, "runs.jsonl", folder="20260901-101900")] == ["r1", "r2"]

    def test_a_separate_press_of_run_gets_its_own_folder(self, store):
        graphs, workspace = store
        workspace.write_run_artifacts("probe", self.state([{"name": "a", "status": "completed", "output": {}}]))
        workspace.write_run_artifacts("probe", {**self.state([{"name": "a", "status": "completed", "output": {}}]),
                                                "run_id": "r2", "created_at": "2026-09-02T08:00:00"})
        folders = sorted(p.name for p in (workspace.workspace_root("probe") / "runs").iterdir())
        assert folders == ["20260901-102030", "20260902-080000"]

    def test_a_node_that_opted_out_keeps_its_output_off_disk(self, store):
        graphs, workspace = store
        workspace.write_run_artifacts("probe", self.state(
            [{"name": "secret", "status": "completed", "output": {"key": "x"}}],
            _save_output_flags={"secret": False}))
        assert self.lines(workspace, "nodes/secret.jsonl")[0]["output"] is None

    def test_a_node_name_cannot_escape_the_folder(self, store):
        graphs, workspace = store
        workspace.write_run_artifacts("probe", self.state(
            [{"name": "../../escape", "status": "completed", "output": {}}]))
        files = listing(workspace, "probe")
        assert "runs/20260901-102030/nodes/escape.jsonl" in files
        assert all(f.startswith("runs/") or "/" not in f or not f.startswith("..") for f in files)

    def test_the_logs_are_files_in_the_workspace(self, store):
        graphs, workspace = store
        workspace.write_run_artifacts("probe", self.state([{"name": "a", "status": "completed", "output": {}}]))
        files = listing(workspace, "probe")
        assert "runs/20260901-102030/nodes/a.jsonl" in files
        assert "runs/20260901-102030/runs.jsonl" in files


class TestDownloading:
    """Getting things back out of the workspace.

    Reading a file for the editor stops at 100KB because that is for looking
    at. Downloading is for keeping, so it hands over the whole thing — and a
    run is a folder, so a folder comes back zipped rather than one file at a
    time.
    """

    @pytest.fixture
    def filled(self, store):
        graphs, workspace = store
        workspace.write_project(graph_with())
        workspace.write_run_artifacts("probe", {
            "run_id": "r1", "status": "success", "created_at": "2026-09-01",
            "inputs": {"country": "Peru"},
            "nodes": [{"name": "first", "status": "completed",
                       "output": {"capital": "Lima"}}],
        })
        return workspace

    def test_a_file_comes_back_whole(self, filled):
        name, media, blob = filled.download("probe", "workflow.py")
        source = (filled.workspace_root("probe") / "workflow.py").read_bytes()

        assert name == "workflow.py"
        assert blob == source
        assert "python" in media

    def test_a_large_file_is_not_truncated(self, filled):
        big = filled.workspace_root("probe") / "files" / "big.txt"
        big.parent.mkdir(parents=True, exist_ok=True)
        big.write_text("x" * (filled.MAX_READ_BYTES * 3), encoding="utf-8")

        _name, _media, blob = filled.download("probe", "files/big.txt")
        # read_file stops at MAX_READ_BYTES; downloading it must not.
        assert len(blob) == filled.MAX_READ_BYTES * 3

    def test_a_run_folder_comes_back_as_a_zip(self, filled):
        import io
        import zipfile

        # The runs folder holds the trace now, not a folder per run.
        name, media, blob = filled.download("probe", "runs")
        inside = zipfile.ZipFile(io.BytesIO(blob)).namelist()

        assert name == "runs.zip" and media == "application/zip"
        assert any(n.endswith("/runs.jsonl") for n in inside)
        assert any("/nodes/" in n for n in inside)

    def test_the_whole_workspace_comes_back_as_a_zip(self, filled):
        import io
        import zipfile

        name, _media, blob = filled.download("probe")
        inside = zipfile.ZipFile(io.BytesIO(blob)).namelist()

        assert name == "probe.zip"
        assert "probe/workflow.py" in inside
        assert any(n.startswith("probe/runs/") and n.endswith("/runs.jsonl") for n in inside)

    def test_a_path_outside_the_workspace_is_refused(self, filled):
        with pytest.raises(filled.WorkspaceError):
            filled.download("probe", "../../../etc/passwd")

    def test_an_unknown_path_says_so(self, filled):
        with pytest.raises(filled.WorkspaceError) as raised:
            filled.download("probe", "nope.txt")
        assert raised.value.not_found


class TestDeleting:
    @pytest.fixture
    def filled(self, store):
        graphs, workspace = store
        root = workspace.workspace_root("probe")
        for rel in ("runs/r1/output.json", "runs/r2/output.json", "notes.txt"):
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")
        (root / "empty").mkdir(parents=True, exist_ok=True)
        return workspace

    def test_a_file_goes(self, filled):
        assert filled.delete_file("probe", "notes.txt")["files"] == 1
        assert not (filled.workspace_root("probe") / "notes.txt").exists()

    def test_an_empty_folder_goes(self, filled):
        outcome = filled.delete_file("probe", "empty")
        assert outcome["directory"] is True and outcome["files"] == 0

    def test_a_folder_with_anything_in_it_is_refused_by_default(self, filled):
        # One careless right-click on runs/ would otherwise take every run this
        # workflow ever produced.
        with pytest.raises(filled.WorkspaceError) as raised:
            filled.delete_file("probe", "runs")
        assert "2 file(s)" in str(raised.value)
        assert (filled.workspace_root("probe") / "runs/r1/output.json").is_file()

    def test_it_goes_when_the_caller_has_said_so(self, filled):
        outcome = filled.delete_file("probe", "runs", recursive=True)
        assert outcome["files"] == 2
        assert not (filled.workspace_root("probe") / "runs").exists()

    def test_deleting_cannot_reach_outside_the_workspace(self, filled):
        with pytest.raises(filled.WorkspaceError):
            filled.delete_file("probe", "../../graphs", recursive=True)


class TestThroughTheWorkspaceApi:
    @pytest.fixture
    def client(self, store):
        from fastapi.testclient import TestClient

        from backend.api import app as studio_app
        graphs, workspace = store
        graphs.create_graph("Probe", "")
        workspace.write_project(graph_with())
        return TestClient(studio_app.app)

    def test_downloading_a_file(self, client):
        res = client.get("/api/graphs/probe/workspace/download",
                         params={"path": "workflow.py"})
        assert res.status_code == 200
        assert 'filename="workflow.py"' in res.headers["content-disposition"]

    def test_downloading_the_workspace(self, client):
        res = client.get("/api/graphs/probe/workspace/download")
        assert res.headers["content-type"] == "application/zip"
        assert 'filename="probe.zip"' in res.headers["content-disposition"]

    def test_a_missing_path_is_a_404(self, client):
        assert client.get("/api/graphs/probe/workspace/download",
                          params={"path": "nope"}).status_code == 404

    def test_deleting_a_full_folder_needs_the_flag(self, client):
        client.put("/api/graphs/probe/workspace/file",
                   json={"path": "runs/r1/output.json", "content": "{}"})

        refused = client.request("DELETE", "/api/graphs/probe/workspace/file",
                                 params={"path": "runs"})
        assert refused.status_code == 400
        assert "removes them too" in refused.json()["detail"]

        allowed = client.request("DELETE", "/api/graphs/probe/workspace/file",
                                 params={"path": "runs", "recursive": "true"})
        assert allowed.status_code == 200
        assert allowed.json()["files"] == 1

class TestAuxiliaryUploads:
    def test_large_checkpoint_is_streamed_and_path_is_usable(self, store):
        import io
        _, workspace = store
        class BoundedStream(io.BytesIO):
            def read(self, size=-1):
                assert 0 < size <= 1024 * 1024
                return super().read(size)
        content = b'x' * (workspace.MAX_WRITE_BYTES + 1)
        result = workspace.upload_file('probe', 'files/model/checkpoint.bin', BoundedStream(content))
        from pathlib import Path
        assert Path(result['absolute_path']).read_bytes() == content
        assert result['size'] == len(content)
        assert any(f.get('absolute_path') == result['absolute_path'] for f in workspace.tree('probe'))

    def test_upload_preserves_existing_files_and_cleans_failed_copies(self, store):
        import io
        _, workspace = store
        root = workspace.workspace_root('probe')
        workspace.upload_file('probe', 'files/model.bin', io.BytesIO(b'original'))
        with pytest.raises(workspace.WorkspaceError, match='already exists'):
            workspace.upload_file('probe', 'files/model.bin', io.BytesIO(b'replacement'))
        assert (root / 'files/model.bin').read_bytes() == b'original'
        class BrokenStream:
            def read(self, size):
                raise OSError('Interrupted upload')
        with pytest.raises(OSError):
            workspace.upload_file('probe', 'files/partial.bin', BrokenStream())
        assert not (root / 'files/partial.bin').exists()
        assert not list(root.rglob('.upload-*'))

    @pytest.mark.parametrize('path', ['../outside.bin', '/outside.bin', 'memory/a.bin', 'datasets/a.bin'])
    def test_upload_respects_workspace_boundaries(self, store, path):
        import io
        _, workspace = store
        with pytest.raises(workspace.WorkspaceError):
            workspace.upload_file('probe', path, io.BytesIO(b'data'))

    def test_binary_checkpoint_has_metadata_without_text_preview(self, store):
        import io
        _, workspace = store
        workspace.upload_file('probe', 'files/model.pt', io.BytesIO(b'\x00\xffweights'))
        result = workspace.read_file('probe', 'files/model.pt')
        assert result['binary'] is True
        assert result['content'] == ''
        assert result['absolute_path'].endswith('/files/model.pt')

    def test_http_upload_uses_streaming_writer(self, store, monkeypatch):
        from fastapi.testclient import TestClient
        from backend.api.app import app
        graphs, workspace = store
        monkeypatch.setattr(graphs, 'graph_exists', lambda _: True)
        monkeypatch.setattr(workspace, 'write_file', lambda *a: pytest.fail('Upload must not use the bounded text writer'))
        with TestClient(app) as client:
            response = client.post('/api/graphs/probe/workspace/upload',
                params={'path':'files/models/nested/weights.bin'},
                files={'file':('weights.bin',b'\x00weights')})
            assert response.status_code == 200, response.text
            assert response.json()['size'] == 8
            assert workspace.read_file('probe','files/models/nested/weights.bin')['binary']
