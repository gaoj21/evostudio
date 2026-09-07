"""Naming a workflow.

A workflow's id is its identity everywhere: the file on disk, the folder its
run artifacts land in, its memory stores, its run history, and the name of the
project it exports as. It used to be fixed at creation, so naming a workflow
only changed a label — you called something "Supplier Risk", handed the export
to a colleague, and they received untitled-workflow-3.zip.
"""

import json

import pytest

from conftest import make_graph, make_task


def body(name, **extra):
    return {**make_graph([make_task("a", outputs=["x"])]), "name": name, **extra}


@pytest.fixture
def store(studio_data, monkeypatch):
    """The graph store, with every directory a rename touches redirected."""
    from studio.backend import batch as batch_module
    from studio.backend import graphs
    from studio.backend import memory_store
    from studio.backend import runner
    from studio.backend import stm_store
    from studio.backend import table_store
    from studio.backend import workspace
    monkeypatch.setattr(workspace, "WORKSPACE_DIR", studio_data / "workspace")
    monkeypatch.setattr(memory_store, "MEMORY_DIR", studio_data / "memory")
    monkeypatch.setattr(table_store, "TABLES_DIR", studio_data / "tables")
    monkeypatch.setattr(stm_store, "STM_DIR", studio_data / "stm")
    monkeypatch.setattr(runner, "RUNS_DIR", studio_data / "runs")
    monkeypatch.setattr(batch_module, "BATCHES_DIR", studio_data / "batches")
    runner._runs.clear()
    batch_module._batches.clear()
    batch_module._threads.clear()
    return graphs


class TestTheIdFollowsTheName:
    def test_naming_a_new_workflow_gives_it_a_matching_id(self, store):
        created = store.create_graph("Untitled Workflow", "")
        assert created["id"] == "untitled-workflow"

        saved = store.save_graph(created["id"], body("Supplier Risk"))
        assert saved["id"] == "supplier-risk"
        assert saved["name"] == "Supplier Risk"

    def test_the_old_id_is_not_left_behind(self, store):
        created = store.create_graph("Untitled Workflow", "")
        store.save_graph(created["id"], body("Supplier Risk"))

        assert store.load_graph("untitled-workflow") is None
        assert store.load_graph("supplier-risk")["name"] == "Supplier Risk"

    def test_saving_without_changing_the_name_leaves_the_id_alone(self, store):
        created = store.create_graph("Supplier Risk", "")
        saved = store.save_graph(created["id"], body("Supplier Risk", goal="new goal"))
        assert saved["id"] == created["id"]
        assert saved["goal"] == "new goal"

    def test_a_name_another_workflow_already_has_keeps_them_apart(self, store):
        store.create_graph("Supplier Risk", "")
        other = store.create_graph("Something Else", "")
        saved = store.save_graph(other["id"], body("Supplier Risk"))

        # Two workflows may share a name; they cannot share an identity.
        assert saved["id"] != "supplier-risk"
        assert store.load_graph("supplier-risk")["name"] == "Supplier Risk"

    def test_a_workflow_whose_id_already_matches_is_not_disturbed(self, store):
        # An older workflow named "Credit Risk" living at credit-risk-2 keeps
        # that id rather than being migrated behind the user's back.
        store.create_graph("Credit Risk", "")
        second = store.create_graph("Credit Risk", "")
        assert second["id"] == "credit-risk-2"

        saved = store.save_graph(second["id"], body("Credit Risk"))
        assert saved["id"] == "credit-risk-2"

    def test_renaming_an_unknown_workflow_is_refused(self, store):
        with pytest.raises(store.GraphValidationError):
            store.rename_graph("ghost", "Something")


class TestItsDataComesWithIt:
    def test_run_artifacts_follow(self, store):
        from studio.backend import workspace
        created = store.create_graph("Untitled Workflow", "")
        artifact = workspace.files_dir(created["id"]) / "note.txt"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("an artifact", encoding="utf-8")

        saved = store.save_graph(created["id"], body("Supplier Risk"))

        moved = workspace.files_dir(saved["id"]) / "note.txt"
        assert moved.read_text(encoding="utf-8") == "an artifact"
        assert not workspace.workspace_root(created["id"]).exists()

    def test_memory_stores_follow(self, store):
        from studio.backend import memory_store
        created = store.create_graph("Untitled Workflow", "")
        db = memory_store.store_dir(created["id"], "judge") / "memory.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_text("{}", encoding="utf-8")

        saved = store.save_graph(created["id"], body("Supplier Risk"))

        # Otherwise a renamed workflow starts from an empty memory, and its old
        # one sits on disk under a name nothing refers to.
        assert (memory_store.store_dir(saved["id"], "judge") / "memory.db").is_file()

    def test_table_memory_follows(self, store):
        from studio.backend import table_store
        created = store.create_graph("Untitled Workflow", "")
        table_store.upsert(created["id"], "judge", "Acme", "2026-01-01",
                           {"outputs": {"verdict": "alert"}}, "now")

        saved = store.save_graph(created["id"], body("Supplier Risk"))

        assert table_store.count(saved["id"], "judge") == 1
        assert table_store.count(created["id"], "judge") == 0

    def test_short_term_memory_follows(self, store):
        from studio.backend import stm_store
        created = store.create_graph("Untitled Workflow", "")
        stm_store.append(created["id"], "morning", {"node": "judge"})

        saved = store.save_graph(created["id"], body("Supplier Risk"))

        assert stm_store.recent(saved["id"], "morning", 10) == [{"node": "judge"}]
        assert stm_store.recent(created["id"], "morning", 10) == []

    def test_run_history_follows(self, store):
        from studio.backend import runner
        created = store.create_graph("Untitled Workflow", "")
        runner._persist_run({"run_id": "r1", "graph_id": created["id"],
                             "status": "success", "nodes": [], "inputs": {}})

        saved = store.save_graph(created["id"], body("Supplier Risk"))

        assert [r["run_id"] for r in runner.list_runs(graph_id=saved["id"])] == ["r1"]
        assert runner.list_runs(graph_id=created["id"]) == []

    def test_batch_history_follows(self, store):
        from studio.backend import batch as batch_module
        created = store.create_graph("Untitled Workflow", "")
        batch_module._persist_batch({"batch_id": "b1", "graph_id": created["id"],
                                     "status": "completed", "items": [], "total": 0})

        saved = store.save_graph(created["id"], body("Supplier Risk"))

        assert [b["batch_id"] for b in batch_module.list_batches(graph_id=saved["id"])] \
            == ["b1"]

    def test_another_workflow_s_history_is_left_alone(self, store):
        from studio.backend import runner
        mine = store.create_graph("Untitled Workflow", "")
        theirs = store.create_graph("Other", "")
        runner._persist_run({"run_id": "r1", "graph_id": mine["id"],
                             "status": "success", "nodes": [], "inputs": {}})
        runner._persist_run({"run_id": "r2", "graph_id": theirs["id"],
                             "status": "success", "nodes": [], "inputs": {}})

        store.save_graph(mine["id"], body("Supplier Risk"))

        assert [r["run_id"] for r in runner.list_runs(graph_id=theirs["id"])] == ["r2"]

    def test_a_run_still_in_memory_is_repointed(self, store):
        from studio.backend import runner
        created = store.create_graph("Untitled Workflow", "")
        runner._runs["r1"] = {"run_id": "r1", "graph_id": created["id"],
                              "status": "success", "nodes": []}

        saved = store.save_graph(created["id"], body("Supplier Risk"))
        assert runner._runs["r1"]["graph_id"] == saved["id"]

    def test_a_workflow_that_has_never_run_renames_cleanly(self, store):
        created = store.create_graph("Untitled Workflow", "")
        assert store.save_graph(created["id"], body("Supplier Risk"))["id"] \
            == "supplier-risk"


class TestWorkInProgress:
    def test_renaming_waits_for_a_running_run(self, store):
        from studio.backend import runner
        created = store.create_graph("Untitled Workflow", "")
        runner._runs["r1"] = {"run_id": "r1", "graph_id": created["id"],
                              "status": "running", "nodes": []}

        # The run is writing into the folder the rename would move.
        with pytest.raises(store.GraphValidationError) as raised:
            store.save_graph(created["id"], body("Supplier Risk"))
        assert "in progress" in str(raised.value)
        assert store.load_graph(created["id"]) is not None

    def test_renaming_waits_for_a_running_batch(self, store):
        from studio.backend import batch as batch_module
        created = store.create_graph("Untitled Workflow", "")
        batch_module._batches["b1"] = {"batch_id": "b1", "graph_id": created["id"],
                                       "status": "running", "items": [], "total": 0,
                                       "created_at": "2026-09-01T00:00:00+00:00",
                                       "source": {}, "metric": None, "summary": None}
        with pytest.raises(store.GraphValidationError):
            store.save_graph(created["id"], body("Supplier Risk"))

    def test_a_finished_run_does_not_block_it(self, store):
        from studio.backend import runner
        created = store.create_graph("Untitled Workflow", "")
        runner._runs["r1"] = {"run_id": "r1", "graph_id": created["id"],
                              "status": "success", "nodes": []}
        assert store.save_graph(created["id"], body("Supplier Risk"))["id"] \
            == "supplier-risk"


class TestThroughTheApi:
    @pytest.fixture
    def client(self, store):
        from fastapi.testclient import TestClient

        from studio.backend import app as studio_app
        return TestClient(studio_app.app)

    def test_the_response_carries_the_new_id(self, client, store):
        created = store.create_graph("Untitled Workflow", "")
        res = client.put(f"/api/graphs/{created['id']}", json=body("Supplier Risk"))

        assert res.status_code == 200
        # The client has to follow this: everything downstream is keyed on it.
        assert res.json()["id"] == "supplier-risk"

    def test_the_old_id_is_gone_from_the_api(self, client, store):
        created = store.create_graph("Untitled Workflow", "")
        client.put(f"/api/graphs/{created['id']}", json=body("Supplier Risk"))

        assert client.get(f"/api/graphs/{created['id']}").status_code == 404
        assert client.get("/api/graphs/supplier-risk").status_code == 200

    def test_the_export_is_named_after_the_workflow(self, client, store):
        import io
        import zipfile

        created = store.create_graph("Untitled Workflow", "")
        saved = client.put(f"/api/graphs/{created['id']}",
                           json=body("Supplier Risk")).json()

        res = client.get(f"/api/graphs/{saved['id']}/export")
        assert 'filename="supplier-risk.zip"' in res.headers["content-disposition"]
        # The folder a colleague unzips, which is the whole point.
        names = zipfile.ZipFile(io.BytesIO(res.content)).namelist()
        assert all(n.startswith("supplier-risk/") for n in names)

    def test_a_rename_blocked_by_a_run_is_reported_not_swallowed(self, client, store):
        from studio.backend import runner
        created = store.create_graph("Untitled Workflow", "")
        runner._runs["r1"] = {"run_id": "r1", "graph_id": created["id"],
                              "status": "running", "nodes": []}
        res = client.put(f"/api/graphs/{created['id']}", json=body("Supplier Risk"))

        assert res.status_code == 422
        assert "in progress" in json.dumps(res.json())
