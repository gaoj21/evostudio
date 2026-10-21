"""Editing and renaming things: what is kept, what follows, and that it answers at once."""
import json
import threading
import time

from fastapi.testclient import TestClient

from backend.api import batch, graphs, runner
from backend.features import persistence
from conftest import make_task


def _wait_refiled(timeout=10):
    deadline = time.time() + timeout
    while graphs._archiving and time.time() < deadline:
        time.sleep(0.01)
    assert not graphs._archiving


def _history(graph_id, run_ids, batch_id):
    for i, run_id in enumerate(run_ids):
        persistence.write_json(runner.RUNS_DIR / f"{run_id}.json", {
            "run_id": run_id, "graph_id": graph_id, "status": "completed",
            "batch_id": batch_id, "created_at": f"2026-01-01T00:00:0{i}"})
    persistence.write_json(batch.BATCHES_DIR / f"{batch_id}.json", {
        "batch_id": batch_id, "graph_id": graph_id, "status": "completed",
        "created_at": "2026-01-01",
        "items": [{"index": i, "run_id": r, "status": "completed"} for i, r in enumerate(run_ids)]},
        indent=None)


def _evolve_result(task_id, graph_id):
    from backend.features.evaluation import evolve_api
    persistence.write_json(evolve_api.EVOLVE_DIR / task_id / "result.json", {
        "task_id": task_id, "graph_id": graph_id, "status": "done", "created_at": "2026-01-01",
        "baseline": {"metrics": {"score": 0.5}}, "optimized": {"metrics": {"score": 0.75}},
        "optimized_graph": {"name": "big", "tasks": [{"name": "t", "prompt": "x" * 1000}]}})


AGENT = "c4" * 16


def chat_agent(name="helper", agent_id=AGENT, **settings):
    return {"name": name, "kind": "chat_agent", "agent_id": agent_id, **settings}


def _sessions(graph_id, agent_id=AGENT):
    from backend.features.agents import harness_api
    return [s["id"] for s in harness_api.list_sessions(graph_id, agent_id)["sessions"]]


def _client():
    from backend.api import app as studio_app
    return TestClient(studio_app.app)


def _body(graph):
    return {k: v for k, v in graph.items() if k != "workflow_inputs"}


class TestRenamingAWorkflow:
    def test_renaming_through_save_keeps_its_chat_agents(self, studio_data):
        """The rename pins the Chat Agents' owner; the save after it dropped
        that, leaving them filed under an id the workflow no longer has."""
        from backend.features.agents import harness_api
        client = _client()
        gid = graphs.create_graph("Agents Kept", "")["id"]
        graphs.save_graph(gid, {"name": "Agents Kept", "tasks": [make_task("a", outputs=["x"]), chat_agent()],
                                "edges": []})
        session = harness_api.create_session(gid, AGENT)

        body = _body(client.get(f"/api/graphs/{gid}").json())
        body["name"] = "Agents Kept Renamed"
        saved = client.put(f"/api/graphs/{gid}", json=body)

        assert saved.status_code == 200
        new_id = saved.json()["id"]
        assert new_id != gid and saved.json()["task_id"] == gid
        assert [t["name"] for t in saved.json()["tasks"] if t.get("kind") == "chat_agent"] == ["helper"]
        assert _sessions(new_id) == [session["id"]]
        _wait_refiled()

    def test_renaming_a_chat_agent_node_keeps_its_conversations(self, studio_data):
        from backend.features.agents import harness_api
        client = _client()
        gid = graphs.create_graph("Node Rename", "")["id"]
        graphs.save_graph(gid, {"name": "Node Rename", "tasks": [chat_agent("scout")], "edges": []})
        session = harness_api.create_session(gid, AGENT)
        body = _body(client.get(f"/api/graphs/{gid}").json())
        body["tasks"][0]["name"] = "researcher"
        saved = client.put(f"/api/graphs/{gid}", json=body).json()
        assert [(t["name"], t["agent_id"]) for t in saved["tasks"]] == [("researcher", AGENT)]
        assert _sessions(gid) == [session["id"]]

    def test_rename_answers_before_its_history_is_refiled_and_lists_it_meanwhile(
            self, studio_data, monkeypatch):
        gid = graphs.create_graph("Long History", "")["id"]
        other = graphs.create_graph("Bystander", "")["id"]
        _history(gid, ["r1", "r2"], "b1")
        _history(other, ["o1"], "b2")
        _evolve_result("e1", gid)
        runner.list_runs(); batch.list_batches()
        untouched = (runner.RUNS_DIR / "o1.json").stat().st_mtime_ns
        gate = threading.Event()
        refile = graphs._repoint_history
        monkeypatch.setattr(graphs, "_repoint_history", lambda *a, **k: (gate.wait(5), refile(*a, **k)))
        client = _client()

        started = time.perf_counter()
        renamed = client.post(f"/api/graphs/{gid}/rename", json={"name": "Short Name"})
        assert renamed.status_code == 200 and time.perf_counter() - started < 2
        new_id = renamed.json()["id"]
        # Not refiled yet, but already its own: every listing and check.
        assert json.loads((runner.RUNS_DIR / "r1.json").read_text())["graph_id"] == gid
        assert {r["run_id"] for r in client.get("/api/runs", params={"graph_id": new_id}).json()} == {"r1", "r2"}
        assert [b["batch_id"] for b in client.get("/api/batches", params={"graph_id": new_id}).json()] == ["b1"]
        assert [t["task_id"] for t in client.get("/api/evolve", params={"graph_id": new_id}).json()] == ["e1"]
        assert client.get("/api/runs", params={"graph_id": gid}).json() == []
        # Its old id is not free while files still carry it.
        assert graphs.free_id("Long History") != gid

        gate.set()
        _wait_refiled()
        for name in ("r1", "r2"):
            assert json.loads((runner.RUNS_DIR / f"{name}.json").read_text())["graph_id"] == new_id
        assert json.loads((batch.BATCHES_DIR / "b1.json").read_text())["graph_id"] == new_id
        from backend.features.evaluation import evolve_api
        assert json.loads((evolve_api.EVOLVE_DIR / "e1" / "result.json").read_text())["graph_id"] == new_id
        assert persistence.cached_view(runner.RUNS_DIR / "r1.json", "run-meta")["graph_id"] == new_id
        assert (runner.RUNS_DIR / "o1.json").stat().st_mtime_ns == untouched
        assert not (graphs.GRAPHS_DIR / ".refiling" / f"{gid}.json").exists()
        assert graphs.free_id("Long History") == gid

    def test_a_second_rename_or_a_delete_during_the_refile_takes_every_file_along(
            self, studio_data, monkeypatch):
        gid = graphs.create_graph("First", "")["id"]
        _history(gid, ["r1", "r2", "r3"], "b1")
        gate = threading.Event()
        refile = graphs._repoint_history
        held = {"first": True}

        def slow(*a, **k):
            if held.pop("first", False):
                gate.wait(5)
            return refile(*a, **k)
        monkeypatch.setattr(graphs, "_repoint_history", slow)

        second = graphs.rename_graph(gid, "Second")["id"]
        third = graphs.rename_graph(second, "Third")["id"]
        assert {r["run_id"] for r in runner.list_runs(graph_id=third)} == {"r1", "r2", "r3"}
        gate.set()
        _wait_refiled()
        assert {json.loads((runner.RUNS_DIR / f"r{i}.json").read_text())["graph_id"] for i in (1, 2, 3)} == {third}
        assert json.loads((batch.BATCHES_DIR / "b1.json").read_text())["graph_id"] == third

        # And a delete while a rename is still refiling archives all of it.
        gate.clear(); held["first"] = True
        fourth = graphs.rename_graph(third, "Fourth")["id"]
        assert graphs.delete_graph(fourth)
        gate.set()
        _wait_refiled()
        ids = {json.loads((runner.RUNS_DIR / f"r{i}.json").read_text())["graph_id"] for i in (1, 2, 3)}
        assert len(ids) == 1 and ids.pop().startswith(f"{fourth}.deleted-")

    def test_a_rename_cut_off_by_a_restart_is_finished_without_archiving_anything(
            self, studio_data):
        from backend.features.agents import harness_api
        gid = graphs.create_graph("Before Restart", "")["id"]
        _history(gid, ["r1"], "b1")
        graphs.save_graph(gid, {"name": "Before Restart", "tasks": [chat_agent("keeper")], "edges": []})
        session = harness_api.create_session(gid, AGENT)
        # As a server stopped right after the rename answered leaves it.
        graph = graphs.load_graph(gid)
        graph.update(id="after-restart", name="After Restart", task_id=gid)
        graphs._save(graph)
        graphs._graph_path(gid).unlink()
        persistence.write_json(graphs._refile_marker(gid), {
            "old_id": gid, "new_id": "after-restart", "task_id": gid, "kind": "rename"})
        graphs._forward_from[0] = None             # a new process
        graphs._archiving.clear()

        assert {r["run_id"] for r in runner.list_runs(graph_id="after-restart")} == {"r1"}
        assert graphs.resume_refiles() == [gid]
        assert json.loads((runner.RUNS_DIR / "r1.json").read_text())["graph_id"] == "after-restart"
        assert _sessions("after-restart") == [session["id"]]
        assert not graphs._refile_marker(gid).exists()

    def test_renaming_is_refused_while_a_source_watch_runs_and_watch_state_follows(
            self, studio_data, monkeypatch):
        from backend.features.execution import watcher
        gid = graphs.create_graph("Watched", "")["id"]
        graphs.save_graph(gid, {"name": "Watched", "tasks": [make_task("feed", outputs=["x"])], "edges": []})
        watcher._save_seen(gid, "feed", {"seen": ["abc"], "fire_count": 3})
        monkeypatch.setattr(watcher, "graph_watch_status", lambda g: {"watching": g == gid})
        refused = _client().post(f"/api/graphs/{gid}/rename", json={"name": "Watched Renamed"})
        assert refused.status_code == 422 and "source watch" in json.dumps(refused.json())

        monkeypatch.setattr(watcher, "graph_watch_status", lambda g: {"watching": False})
        new_id = graphs.rename_graph(gid, "Watched Renamed")["id"]
        assert watcher._load_seen(new_id, "feed") == {"seen": ["abc"], "fire_count": 3}
        _wait_refiled()

    def test_the_schedule_follows_every_rename(self, studio_data):
        from backend.features.execution import scheduler
        gid = graphs.create_graph("Scheduled", "")["id"]
        scheduler._save(gid, {"enabled": False, "mode": "interval", "minutes": 30})
        new_id = graphs.rename_graph(gid, "Scheduled Renamed")["id"]
        assert scheduler.load(gid) is None and scheduler.load(new_id)["minutes"] == 30
        _wait_refiled()


class TestSavingAWorkflow:
    def test_large_non_ascii_edits_round_trip_and_rapid_saves_never_corrupt(self, studio_data):
        client = _client()
        gid = graphs.create_graph("Round Trip", "")["id"]
        prompt = '中文提示词 ✓ "quotes" {{braces}} \\ backslash {x}\n' * 3000
        body = {"name": "Round Trip", "tasks": [make_task("a", inputs=["x"], outputs=["y"], prompt=prompt)],
                "edges": []}
        bodies = [{**body, "goal": f"目标 {i}"} for i in range(8)]
        threads = [threading.Thread(target=client.put, args=(f"/api/graphs/{gid}",), kwargs={"json": b})
                   for b in bodies]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        saved = json.loads(graphs._graph_path(gid).read_text(encoding="utf-8"))
        assert saved["tasks"][0]["prompt"] == prompt
        assert saved["goal"] in {b["goal"] for b in bodies}
        assert not list(graphs.GRAPHS_DIR.glob("*.tmp"))
        assert client.get(f"/api/graphs/{gid}").json()["tasks"][0]["prompt"] == prompt

    def test_an_invalid_save_is_a_422_and_leaves_the_saved_workflow_alone(self, studio_data):
        client = _client()
        gid = graphs.create_graph("Stays", "")["id"]
        graphs.save_graph(gid, {"name": "Stays", "tasks": [make_task("a", outputs=["x"])], "edges": []})
        before = graphs._graph_path(gid).read_text(encoding="utf-8")
        bad = client.put(f"/api/graphs/{gid}", json={"name": "Stays Renamed", "output_dir": "../outside",
                                                     "tasks": [make_task("a", outputs=["x"])], "edges": []})
        assert bad.status_code == 422
        assert graphs._graph_path(gid).read_text(encoding="utf-8") == before
        assert graphs.graph_exists(gid) and not graphs.graph_exists("stays-renamed")


class TestApplyingAnEvolveResult:
    def test_an_applied_copy_has_its_own_chat_agents(self, studio_data):
        """The copy took the original's task_id: the two shared Chat Agents,
        and deleting the copy archived the original's. Now the copies carry
        the Chat Agent node, with conversations of their own."""
        from backend.features.agents import harness_api
        from backend.features.evaluation import evolve_api
        gid = graphs.create_graph("Evolved Source", "")["id"]
        graph = graphs.save_graph(gid, {"name": "Evolved Source", "task_id": "t-original",
                                        "tasks": [make_task("a", outputs=["x"]), chat_agent()], "edges": []})
        session = harness_api.create_session(gid, AGENT)
        persistence.write_json(evolve_api.EVOLVE_DIR / "ev1" / "result.json", {
            "task_id": "ev1", "graph_id": gid, "status": "done", "optimized_graph": graph,
            "diff": [{"name": "a", "changed": True}]})

        copy = evolve_api.apply_evolve_result("ev1", mode="new")
        replaced = evolve_api.apply_evolve_result("ev1", mode="replace")
        backup = graphs.load_graph(replaced["backup_id"])
        for other in (copy, backup):
            assert [t["agent_id"] for t in other["tasks"] if t.get("kind") == "chat_agent"] == [AGENT]
            assert _sessions(other["id"]) == []
        mine = harness_api.create_session(copy["id"], AGENT)
        assert _sessions(copy["id"]) == [mine["id"]] and _sessions(gid) == [session["id"]]
        graphs.delete_graph(copy["id"])
        graphs.delete_graph(replaced["backup_id"])
        _wait_refiled()
        assert _sessions(gid) == [session["id"]]

    def test_the_evolve_list_does_not_parse_every_result_on_every_call(self, studio_data, monkeypatch):
        from backend.features.evaluation import evolve_api
        _evolve_result("e1", "listed")
        first = evolve_api.list_evolve_tasks("listed")
        assert first[0]["baseline_score"] == 0.5 and first[0]["optimized_score"] == 0.75
        persistence.clear_memory_views()            # a restart: the disk cache stays
        monkeypatch.setattr(json, "load", lambda *a, **k: (_ for _ in ()).throw(AssertionError("parsed")))
        assert evolve_api.list_evolve_tasks("listed") == first


class TestResolvingAReview:
    def test_the_outcome_lands_on_its_own_batch_only_and_survives_the_journal(self, studio_data):
        from backend.features.execution import review
        _history("g", ["r1", "r2"], "b1")
        _history("g", ["x1"], "b2")
        # The item settled after the last snapshot: its review status is journaled.
        batch._journal_path("b1").write_text(
            json.dumps({"index": 0, "run_id": "r1", "review_status": "awaiting_review"}) + "\n")
        other = (batch.BATCHES_DIR / "b2.json").stat().st_mtime_ns
        persistence.write_json(review.REVIEWS_DIR / "rv1.json", {
            "review_id": "rv1", "run_id": "r1", "graph_id": "g", "status": "pending",
            "approve_label": "alert", "reject_label": "suppress"})

        resolved = review.resolve_review("rv1", "approve", note="已确认")

        assert resolved["final_action"] == "alert"
        item = batch.get_batch("b1")["items"][0]
        assert item["review_status"] == "approved" and item["final_action"] == "alert"
        assert json.loads((runner.RUNS_DIR / "r1.json").read_text())["review_note"] == "已确认"
        assert (batch.BATCHES_DIR / "b2.json").stat().st_mtime_ns == other
        assert review.get_review("rv1")["status"] == "approved"


class TestMalformedAndImportRoutes:
    def test_a_malformed_body_is_a_422_not_a_500(self, studio_data):
        client = _client()
        gid = graphs.create_graph("Shape", "")["id"]
        for body in ({"tasks": "x"}, {"tasks": [1, 2]}, {"edges": {"a": 1}}, {"name": 5, "tasks": []}):
            assert client.put(f"/api/graphs/{gid}", json=body).status_code == 422, body

    def test_import_answers_where_the_ui_posts_it(self, studio_data):
        """It was mounted under /api/api/graphs/import: Import answered 405."""
        graph = {"name": "Imported 导入", "goal": "", "tasks": [make_task("a", outputs=["x"])], "edges": []}
        response = _client().post("/api/graphs/import", files={
            "file": ("workflow.json", json.dumps(graph, ensure_ascii=False).encode(), "application/json")})
        assert response.status_code == 200, response.text
        assert graphs.load_graph(response.json()["id"])["name"].startswith("Imported")


class TestIdsAndFollowUps:
    def test_non_ascii_names_keep_a_stable_tag_instead_of_collapsing(self, studio_data):
        from backend.features.chat.chat_api import _slug
        assert graphs._slugify("信用风险") == graphs._slugify("信用风险") != graphs._slugify("供应商")
        assert graphs._slugify("信用风险").startswith("graph-")
        assert graphs._slugify("Plain Name") == "plain-name" and _slug("Plain Name") == "plain_name"
        for name in ("信用风险", "任务 {x}", "Café"):
            assert persistence.is_safe_id(graphs._slugify(name))
        # The frontend's sluggify() gives exactly these (convert.test.js).
        assert _slug("任务 {x}") == "x_65c975" and _slug("信用风险") == "node_6a9693"

    def test_a_deleted_workflows_watch_state_is_archived(self, studio_data):
        from backend.features.execution import watcher
        gid = graphs.create_graph("Watch Gone", "")["id"]
        graphs.save_graph(gid, {"name": "Watch Gone", "tasks": [make_task("feed", outputs=["x"])], "edges": []})
        watcher._save_seen(gid, "feed", {"seen": ["abc"], "fire_count": 1})
        assert graphs.delete_graph(gid)
        _wait_refiled()
        assert watcher._load_seen(gid, "feed") == {"seen": [], "fire_count": 0}
        assert list(watcher.WATCH_DIR.glob(".deleted-*/*.json"))

    def test_chat_saves_compile_the_workspace_as_the_save_button_does(self, studio_data, monkeypatch):
        from backend.api import workspace
        from backend.features.chat import chat_api
        gid = graphs.create_graph("Chat Saved", "")["id"]
        compiled = []
        monkeypatch.setattr(workspace, "write_project", lambda g: compiled.append(g["id"]))
        chat_api._save_graph(gid, {"name": "Chat Saved", "tasks": [make_task("a", outputs=["x"])], "edges": []})
        assert compiled == [gid]

    def test_the_refile_rereads_a_file_changed_under_it(self, studio_data, monkeypatch):
        gid = graphs.create_graph("Raced", "")["id"]
        _history(gid, ["r1"], "b1")
        path = runner.RUNS_DIR / "r1.json"
        real = persistence.file_version
        calls = {"n": 0}

        def racing(p):
            # Another writer lands between the first read and its check.
            if str(p) == str(path) and calls["n"] == 0:
                calls["n"] += 1
                version = real(p)
                persistence.write_json(path, {**json.loads(path.read_text()), "review_note": "kept"})
                return version
            return real(p)
        monkeypatch.setattr(persistence, "file_version", racing)
        new_id = graphs.rename_graph(gid, "Raced Renamed")["id"]
        _wait_refiled()
        data = json.loads(path.read_text())
        assert data["graph_id"] == new_id and data["review_note"] == "kept"
