"""Deleting things: what goes, what is kept, and that it answers at once."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from backend.api import batch, graphs, runner
from backend.features import persistence


def _wait_archived(timeout=10):
    deadline = time.time() + timeout
    while graphs._archiving and time.time() < deadline:
        time.sleep(0.01)
    assert not graphs._archiving


def _history(graph_id, run_ids, batch_id):
    for run_id in run_ids:
        persistence.write_json(runner.RUNS_DIR / f"{run_id}.json", {
            "run_id": run_id, "graph_id": graph_id, "status": "completed",
            "created_at": f"2026-01-01T00:00:0{run_ids.index(run_id)}"})
    persistence.write_json(batch.BATCHES_DIR / f"{batch_id}.json", {
        "batch_id": batch_id, "graph_id": graph_id, "status": "completed",
        "created_at": "2026-01-01", "items": []}, indent=None)


class TestDeletingAWorkflow:
    def test_only_its_own_history_is_refiled_and_the_listings_follow(self, studio_data):
        doomed = graphs.create_graph("Doomed", "")["id"]
        kept = graphs.create_graph("Kept", "")["id"]
        _history(doomed, ["r1", "r2"], "b1")
        _history(kept, ["k1"], "b2")
        # The listings have looked at every file once, as a running server has.
        runner.list_runs(); batch.list_batches()
        other = (runner.RUNS_DIR / "k1.json").stat().st_mtime_ns, (batch.BATCHES_DIR / "b2.json").stat().st_mtime_ns

        assert graphs.delete_graph(doomed)
        _wait_archived()

        assert runner.list_runs(graph_id=doomed) == []
        assert batch.list_batches(graph_id=doomed) == []
        archived = {json.loads((runner.RUNS_DIR / f"{r}.json").read_text())["graph_id"] for r in ("r1", "r2")}
        assert len(archived) == 1 and archived.pop().startswith(f"{doomed}.deleted-")
        assert json.loads((batch.BATCHES_DIR / "b1.json").read_text())["graph_id"].startswith(f"{doomed}.deleted-")
        # Another workflow's files are not even rewritten.
        assert ((runner.RUNS_DIR / "k1.json").stat().st_mtime_ns,
                (batch.BATCHES_DIR / "b2.json").stat().st_mtime_ns) == other
        # The cached views were updated with the files: nothing is parsed again.
        meta = persistence.cached_view(runner.RUNS_DIR / "r1.json", "run-meta")
        assert meta["graph_id"].startswith(f"{doomed}.deleted-")
        assert [r["run_id"] for r in runner.list_runs(graph_id=kept)] == ["k1"]

    def test_the_id_stays_taken_until_its_history_is_refiled(self, studio_data, monkeypatch):
        doomed = graphs.create_graph("Doomed", "")["id"]
        _history(doomed, ["r1"], "b1")
        gate = __import__("threading").Event()
        refile = graphs._repoint_history
        monkeypatch.setattr(graphs, "_repoint_history", lambda *a, **k: (gate.wait(5), refile(*a, **k)))

        started = time.perf_counter()
        assert graphs.delete_graph(doomed)
        assert time.perf_counter() - started < 1      # answered before the refile
        assert graphs.create_graph("Doomed", "")["id"] == "doomed-2"
        # Its history, still filed under the id, is not listed meanwhile.
        from backend.api import app as studio_app
        client = TestClient(studio_app.app)
        assert client.get(f"/api/runs?graph_id={doomed}").json() == []
        assert client.get(f"/api/batches?graph_id={doomed}").json() == []
        gate.set()
        _wait_archived()
        assert graphs.create_graph("Doomed", "")["id"] == "doomed"

    def test_rename_rewrites_only_its_own_runs(self, studio_data):
        mine = graphs.create_graph("Mine", "")["id"]
        other = graphs.create_graph("Other", "")["id"]
        _history(mine, ["m1"], "b1")
        _history(other, ["o1"], "b2")
        before = (runner.RUNS_DIR / "o1.json").stat().st_mtime_ns
        renamed = graphs.rename_graph(mine, "Renamed")["id"]
        assert [r["run_id"] for r in runner.list_runs(graph_id=renamed)] == ["m1"]
        assert [b["batch_id"] for b in batch.list_batches(graph_id=renamed)] == ["b1"]
        assert (runner.RUNS_DIR / "o1.json").stat().st_mtime_ns == before


class TestMemoryResetStaysInsideTheStores:
    def test_an_id_that_climbs_out_is_refused(self, studio_data, tmp_path):
        from backend.api import app as studio_app
        from backend.api import memory_reset, studio_config
        sibling = studio_config.data_path("memory") / ".." / "precious"
        sibling.mkdir(parents=True, exist_ok=True)
        (sibling / "keep.txt").write_text("x")
        client = TestClient(studio_app.app)
        for path in ("/api/memory/reset", "/api/memory/backup"):
            res = client.post(path, json={"graph_id": "../precious", "backup": False})
            assert res.status_code == 400, res.text
        assert (sibling / "keep.txt").is_file()
        with pytest.raises(persistence.InvalidId):
            memory_reset.clear("../precious", backup=False)


class TestMissingThingsAre404:
    def test_schedule_and_watch_of_an_unknown_workflow(self, studio_data):
        from backend.api import app as studio_app
        client = TestClient(studio_app.app)
        assert client.delete("/api/graphs/nope/schedule").status_code == 404
        assert client.delete("/api/graphs/nope/watch").status_code == 404
        gid = graphs.create_graph("Here", "")["id"]
        assert client.delete(f"/api/graphs/{gid}/schedule").json() == {"ok": True, "removed": False}
        assert client.delete(f"/api/graphs/{gid}/watch").status_code == 200

    def test_mem0_space_and_entry(self, tmp_path, monkeypatch):
        from unittest.mock import Mock
        from fastapi import HTTPException
        from backend.api import mem0_api, mem0_service as service
        monkeypatch.setattr(service, "ROOT", tmp_path / "mem0")
        db = Mock()
        db.get.return_value = None
        monkeypatch.setattr(service, "_client", db)
        graph = {"id": "a", "project_id": "p"}
        monkeypatch.setattr(mem0_api, "graph_for", lambda g: graph)
        space = service.create_space(graph, "S")["id"]
        with pytest.raises(HTTPException) as missing_entry:
            mem0_api.delete_entry("a", space, "nope")
        assert missing_entry.value.status_code == 404
        with pytest.raises(HTTPException) as missing_space:
            mem0_api.delete_space("a", "0" * 32)
        assert missing_space.value.status_code == 404


def test_deleting_a_space_does_not_wait_for_mem0_to_start(tmp_path, monkeypatch):
    """Starting Mem0 loads the embedding model; deleting a space must not."""
    import threading
    from unittest.mock import Mock
    from backend.api import mem0_api, mem0_service as service
    monkeypatch.setattr(service, "ROOT", tmp_path / "mem0")
    monkeypatch.setattr(service, "_client", None)
    graph = {"id": "a", "project_id": "p"}
    monkeypatch.setattr(mem0_api, "graph_for", lambda g: graph)
    monkeypatch.setattr(mem0_api.graphs, "list_graphs", lambda: [])
    space = service.create_space(graph, "S")["id"]
    started, db = threading.Event(), Mock()

    def slow_client():
        started.wait(5)
        return db
    monkeypatch.setattr(service, "client", slow_client)

    begun = time.perf_counter()
    assert mem0_api.delete_space("a", space) == {"removed": True}
    assert time.perf_counter() - begun < 1
    assert service.spaces(graph) == []          # gone before Mem0 started
    started.set()
    deadline = time.time() + 5
    while not db.delete_all.called and time.time() < deadline:
        time.sleep(0.01)
    db.delete_all.assert_called_once_with(user_id=space)


def test_discarded_candidate_batches_leave_no_cached_views(tmp_path):
    from backend.features.evaluation import canvas_evolution
    persistence.write_json(runner.RUNS_DIR / "cr.json", {"run_id": "cr", "graph_id": "g"})
    persistence.write_json(batch.BATCHES_DIR / "cb.json", {
        "batch_id": "cb", "graph_id": "g", "items": [{"run_id": "cr", "status": "completed"}]}, indent=None)
    runner.list_runs(); batch.list_batches()
    canvas_evolution.discard_candidate_batches(["cb"])
    assert not (runner.RUNS_DIR / "cr.json").exists()
    assert not (batch.BATCHES_DIR / "cb.json").exists()
    assert not any(str(runner.RUNS_DIR / "cr.json") == k[1] or str(batch.BATCHES_DIR / "cb.json") == k[1]
                   for k in persistence._views)
    assert "cr" not in {r["run_id"] for r in runner.list_runs(limit=None)}
    assert "cb" not in {b["batch_id"] for b in batch.list_batches()}


def test_a_run_still_in_memory_has_its_file_refiled_too(studio_data):
    """The listing takes a run held in memory from memory; its file on disk
    was then skipped and kept the deleted workflow's id."""
    doomed = graphs.create_graph("Doomed", "")["id"]
    _history(doomed, ["m1"], "b9")
    runner._runs["m1"] = {"run_id": "m1", "graph_id": doomed, "status": "completed", "nodes": []}

    assert graphs.delete_graph(doomed)
    _wait_archived()

    on_disk = json.loads((runner.RUNS_DIR / "m1.json").read_text())["graph_id"]
    assert on_disk.startswith(f"{doomed}.deleted-")
    assert runner._runs["m1"]["graph_id"] == on_disk


class TestARefileSurvivesARestart:
    """The refile after a delete runs in the background; a server stopped in
    the middle of it finishes it at the next start."""

    def test_a_marker_is_written_before_the_refile_and_removed_after(self, studio_data, monkeypatch):
        import threading
        doomed = graphs.create_graph("Doomed", "")["id"]
        _history(doomed, ["r1"], "b1")
        gate = threading.Event()
        refile = graphs._repoint_history
        monkeypatch.setattr(graphs, "_repoint_history", lambda *a, **k: (gate.wait(5), refile(*a, **k)))

        assert graphs.delete_graph(doomed)
        marker = graphs._refile_marker(doomed)
        written = json.loads(marker.read_text())
        assert written["old_id"] == doomed
        assert written["new_id"].startswith(f"{doomed}.deleted-") and written["started_at"]
        gate.set()
        _wait_archived()
        assert not marker.exists()
        # The graph listing does not mistake the marker folder for a workflow.
        assert [g["id"] for g in graphs.list_graphs()] == []

    def test_startup_finishes_a_half_done_refile_and_keeps_the_id_taken(self, studio_data, monkeypatch):
        import threading
        doomed = graphs.create_graph("Doomed", "")["id"]
        _history(doomed, ["r1", "r2"], "b1")
        archived = f"{doomed}.deleted-20260101T000000000000Z"
        # What a server stopped mid-refile leaves: the graph gone, one run
        # refiled, the rest still under the old id, and the marker.
        graphs._graph_path(doomed).unlink()
        data = json.loads((runner.RUNS_DIR / "r1.json").read_text())
        persistence.write_json(runner.RUNS_DIR / "r1.json", {**data, "graph_id": archived})
        persistence.write_json(graphs._refile_marker(doomed), {
            "old_id": doomed, "new_id": archived, "started_at": "2026-01-01T00:00:00"})
        before = (runner.RUNS_DIR / "r1.json").stat().st_mtime_ns

        # A fresh process: nothing in memory says the id is being archived,
        # yet it is taken and its leftover history is not listed.
        assert not graphs._archiving
        assert graphs.is_archiving(doomed)
        assert graphs.create_graph("Doomed", "")["id"] == "doomed-2"
        from backend.api import app as studio_app
        client = TestClient(studio_app.app)
        assert client.get(f"/api/runs?graph_id={doomed}").json() == []

        gate, seen = threading.Event(), []
        refile = graphs._repoint_history

        def held(*a, **k):
            seen.append(doomed in graphs._archiving)
            gate.wait(5)
            return refile(*a, **k)
        monkeypatch.setattr(graphs, "_repoint_history", held)
        worker = threading.Thread(target=graphs.resume_refiles)
        worker.start()
        deadline = time.time() + 5
        while not seen and time.time() < deadline:
            time.sleep(0.01)
        assert seen == [True]                    # held while it runs
        assert graphs.create_graph("Doomed", "")["id"] == "doomed-3"
        gate.set()
        worker.join(5)

        assert not graphs._refile_marker(doomed).exists()
        assert not graphs.is_archiving(doomed)
        for run_id in ("r1", "r2"):
            assert json.loads((runner.RUNS_DIR / f"{run_id}.json").read_text())["graph_id"] == archived
        assert json.loads((batch.BATCHES_DIR / "b1.json").read_text())["graph_id"] == archived
        # Idempotent: the run refiled before the stop was not rewritten.
        assert (runner.RUNS_DIR / "r1.json").stat().st_mtime_ns == before
        assert graphs.create_graph("Doomed", "")["id"] == "doomed"

    def test_a_stop_before_the_graph_file_went_deletes_nothing(self, studio_data):
        """The marker is written first; stopped right after it, the delete
        never happened, and the workflow and its history stay as they were."""
        kept = graphs.create_graph("Kept", "")["id"]
        _history(kept, ["r1"], "b1")
        persistence.write_json(graphs._refile_marker(kept), {
            "old_id": kept, "new_id": f"{kept}.deleted-20260101T000000000000Z",
            "started_at": "2026-01-01T00:00:00"})

        graphs.resume_refiles()

        assert graphs.load_graph(kept) is not None
        assert not graphs._refile_marker(kept).exists() and not graphs.is_archiving(kept)
        assert json.loads((runner.RUNS_DIR / "r1.json").read_text())["graph_id"] == kept

    def test_a_stop_before_its_data_was_moved_finishes_the_delete(self, studio_data):
        """Stopped after the graph file went but before anything was set
        aside: the next start moves its data and refiles its history."""
        from backend.api import table_store
        doomed = graphs.create_graph("Doomed", "")["id"]
        _history(doomed, ["r1"], "b1")
        tables = table_store.TABLES_DIR / doomed
        tables.mkdir(parents=True)
        (tables / "t.db").write_text("x")
        archived = f"{doomed}.deleted-20260101T000000000000Z"
        persistence.write_json(graphs._refile_marker(doomed), {
            "old_id": doomed, "new_id": archived, "started_at": "2026-01-01T00:00:00"})
        graphs._graph_path(doomed).unlink()

        graphs.resume_refiles()

        assert not tables.exists()
        assert (table_store.TABLES_DIR / ".deleted-20260101T000000000000Z" / doomed / "t.db").is_file()
        assert json.loads((runner.RUNS_DIR / "r1.json").read_text())["graph_id"] == archived
        assert not graphs._refile_marker(doomed).exists() and not graphs.is_archiving(doomed)

    def test_the_server_start_resumes_it(self, studio_data):
        doomed = graphs.create_graph("Doomed", "")["id"]
        _history(doomed, ["r1"], "b1")
        graphs._graph_path(doomed).unlink()
        archived = f"{doomed}.deleted-20260101T000000000000Z"
        persistence.write_json(graphs._refile_marker(doomed), {
            "old_id": doomed, "new_id": archived, "started_at": "x"})
        from backend.api import app as studio_app
        with TestClient(studio_app.app):
            deadline = time.time() + 10
            while graphs._refile_marker(doomed).exists() and time.time() < deadline:
                time.sleep(0.02)
        assert not graphs._refile_marker(doomed).exists()
        assert json.loads((runner.RUNS_DIR / "r1.json").read_text())["graph_id"] == archived

    def test_an_unreadable_marker_is_left_alone(self, studio_data):
        marker = graphs._refile_marker("x")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{not json")
        assert graphs.resume_refiles() == []
        assert marker.exists()


class TestDeletingADataResource:
    @pytest.fixture
    def data_dir(self, tmp_path, monkeypatch):
        from backend.features.data import data_resources
        monkeypatch.setattr(data_resources, "data_path", lambda *parts: tmp_path.joinpath(*parts))
        monkeypatch.setattr(graphs, "GRAPHS_DIR", tmp_path / "graphs")
        (tmp_path / "graphs").mkdir()
        return tmp_path

    def test_a_corrupt_workflow_file_is_skipped_not_a_500(self, data_dir, caplog):
        from backend.features.data import data_resources
        resource = data_resources.create_from_records([{"a": 1}], "r")
        (data_dir / "graphs" / "broken.json").write_text("{oops")
        (data_dir / "graphs" / "list.json").write_text("[1, 2]")
        assert data_resources.remove(resource["id"]) == {"deleted": resource["id"]}
        assert not data_resources.path_for(resource["id"]).exists()
        assert "broken.json" in caplog.text
        assert not any(p.name.startswith(".deleted-") for p in data_resources.directory().iterdir())

    def test_a_used_resource_is_still_refused(self, data_dir):
        from fastapi import HTTPException
        from backend.features.data import data_resources
        resource = data_resources.create_from_records([{"a": 1}], "r")
        (data_dir / "graphs" / "broken.json").write_text("{oops")
        persistence.write_json(data_dir / "graphs" / "user.json", {
            "id": "user", "name": "User", "tasks": [{"name": "s", "source": {"resource_id": resource["id"]}}]})
        with pytest.raises(HTTPException) as refused:
            data_resources.remove(resource["id"])
        assert refused.value.status_code == 409 and "User" in refused.value.detail

    def test_an_attach_cannot_land_between_the_check_and_the_delete(self, data_dir, monkeypatch):
        import threading
        from fastapi import HTTPException
        from backend.features.data import data_resources
        resource = data_resources.create_from_records([{"a": 1}], "r")
        persistence.write_json(data_dir / "graphs" / "g.json", {"id": "g", "name": "G", "tasks": [], "edges": []})
        checked, release = threading.Event(), threading.Event()
        used_by = data_resources._used_by

        def slow_check(rid):
            out = used_by(rid)
            checked.set()
            release.wait(5)
            return out
        monkeypatch.setattr(data_resources, "_used_by", slow_check)
        deleting = threading.Thread(target=data_resources.remove, args=(resource["id"],))
        deleting.start()
        assert checked.wait(5)
        outcome = {}

        def attach():
            try:
                outcome["ok"] = data_resources.attach_workspace(resource["id"], {"graph_id": "g"})
            except HTTPException as exc:
                outcome["status"] = exc.status_code
        attaching = threading.Thread(target=attach)
        attaching.start()
        time.sleep(0.1)
        assert attaching.is_alive()              # waits for the delete
        release.set()
        deleting.join(5); attaching.join(5)
        assert outcome == {"status": 404}        # not a 500, not a half-written folder
        assert not data_resources.path_for(resource["id"]).exists()


class TestDeletingAWorkspaceFolder:
    def test_a_folder_is_renamed_aside_and_removed_in_the_background(self, studio_data, monkeypatch):
        import threading
        from backend.api import workspace
        gid = graphs.create_graph("W", "")["id"]
        folder = workspace.workspace_root(gid) / "big"
        (folder / "sub").mkdir(parents=True)
        for i in range(5):
            (folder / "sub" / f"{i}.txt").write_text("x")
        gate, removing = threading.Event(), []
        real = workspace._remove_trash

        def held(path):
            removing.append(path)
            gate.wait(5)
            real(path)
        monkeypatch.setattr(workspace, "_remove_trash", held)

        out = workspace.delete_file(gid, "big", recursive=True)
        assert out == {"deleted": "big", "files": None, "directory": True}
        assert not folder.exists()
        trash = workspace.workspace_root(gid) / workspace.TRASH_DIR_NAME
        assert trash.is_dir() and any(trash.iterdir())       # not removed yet
        listed = [e["path"] for e in workspace.tree(gid)]
        assert not any(workspace.TRASH_DIR_NAME in p or p.startswith("big") for p in listed)
        with pytest.raises(workspace.WorkspaceError):
            workspace.read_file(gid, f"{workspace.TRASH_DIR_NAME}/x")
        _, _, body = workspace.download(gid, "")
        import io, zipfile
        assert not any(workspace.TRASH_DIR_NAME in n for n in zipfile.ZipFile(io.BytesIO(body)).namelist())
        gate.set()
        deadline = time.time() + 5
        while trash.exists() and time.time() < deadline:
            time.sleep(0.01)
        assert not trash.exists()

    def test_leftover_trash_is_emptied_at_startup(self, studio_data):
        from backend.api import workspace
        gid = graphs.create_graph("W", "")["id"]
        left = workspace.workspace_root(gid) / workspace.TRASH_DIR_NAME / "abc-old"
        left.mkdir(parents=True)
        (left / "f.txt").write_text("x")
        keep = workspace.workspace_root(gid) / "keep.txt"
        keep.write_text("k")
        assert workspace.empty_trash() == 1
        assert not left.parent.exists() and keep.is_file()


def test_a_deleted_workflows_evaluator_draft_is_archived_with_it(studio_data):
    from backend.api import evaluator_tools
    gid = graphs.create_graph("Drafty", "")["id"]
    evaluator_tools.put_draft(gid, "def evaluate(records): return {}")
    assert graphs.delete_graph(gid)
    assert evaluator_tools.get_draft(gid) is None
    assert list(evaluator_tools.DRAFTS_DIR.glob(f".deleted-*/{gid}.json"))
    assert graphs.create_graph("Drafty", "")["id"] == gid


def test_a_deleted_workflows_chat_agents_are_archived_not_inherited(studio_data):
    """Chat Agent conversations live in the harness store under the
    workflow's task id; left there, a new workflow of the same name (and a
    Chat Agent node of the same agent_id) opened with them."""
    from backend.features.agents import harness_api
    doomed = graphs.create_graph("Doomed", "")["id"]
    agent = {"id": "d0" * 16}
    graphs.save_graph(doomed, {"name": "Doomed", "edges": [], "tasks": [
        {"name": "helper", "kind": "chat_agent", "agent_id": agent["id"]}]})
    session = harness_api.create_session(doomed, agent["id"])
    with harness_api.database() as db:              # a definition left from before the migration
        harness_api.put(db, agent["id"], doomed, "migrated-agent", "", {"id": agent["id"]})

    assert graphs.delete_graph(doomed)
    _wait_archived()

    reborn = graphs.create_graph("Doomed", "")["id"]
    assert reborn == doomed
    assert harness_api.list_sessions(reborn, agent["id"])["sessions"] == []
    with harness_api.database() as db:
        rows = db.execute("SELECT owner, kind FROM objects WHERE id IN (?, ?)",
                          (agent["id"], session["id"])).fetchall()
    assert len(rows) == 2 and all(owner.startswith(f"{doomed}.deleted-") for owner, _ in rows)
