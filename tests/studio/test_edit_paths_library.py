"""Saving things: what a bad request gets back, and that a save is whole."""
import json
import threading

import pytest
from fastapi.testclient import TestClient

from backend.api import graphs


def _client():
    from backend.api import app as studio_app
    return TestClient(studio_app.app)


TOOL = '''"""Greets in Chinese."""
def greet_cn(name: str) -> str:
    """Say hi.

    Args:
        name: who
    """
    return "你好, " + name
'''


class TestCustomTools:
    def test_wrong_types_are_422_not_500(self, studio_data):
        client = _client()
        for body in ({"code": TOOL, "name": 123}, {"code": 123}, {"code": TOOL, "sources": 5},
                     {"code": TOOL, "sources": "greet_cn"}, {"code": TOOL, "config": "x"}):
            res = client.post("/api/tools/custom", json=body)
            assert res.status_code == 422, (body, res.text)
        res = client.post("/api/tools/custom/preview", json={"code": TOOL, "calls": [5]})
        assert res.status_code == 422, res.text

    def test_a_failed_write_keeps_the_saved_toolkit(self, studio_data, monkeypatch):
        from backend.api import custom_tools
        client = _client()
        assert client.post("/api/tools/custom", json={"code": TOOL}).status_code == 200
        before = (custom_tools.TOOLS_DIR / "greet_cn.json").read_text(encoding="utf-8")
        assert "你好" in before

        def broken(source, destination):
            raise OSError("disk full")
        monkeypatch.setattr("backend.features.persistence.replace", broken)
        with pytest.raises(OSError):
            client.post("/api/tools/custom", json={"code": TOOL.replace("你好", "再见")})
        assert (custom_tools.TOOLS_DIR / "greet_cn.json").read_text(encoding="utf-8") == before
        assert [p.name for p in custom_tools.TOOLS_DIR.iterdir()] == ["greet_cn.json"]

    def test_an_invalid_entry_edit_leaves_the_folder_file(self, studio_data):
        import io
        import zipfile
        from backend.api import custom_tools
        client = _client()
        blob = io.BytesIO()
        with zipfile.ZipFile(blob, "w") as archive:
            archive.writestr("kit/tools.py", '"""kit"""\ndef kit_a(x: int) -> int:\n    """a"""\n    return x\n')
        assert client.post("/api/tools/custom/upload",
                           files={"file": ("kit.zip", blob.getvalue(), "application/zip")}).status_code == 200
        entry = custom_tools.package_dir("kit") / "tools.py"
        before = entry.read_text(encoding="utf-8")
        res = client.post("/api/tools/custom", json={"name": "kit", "code": "def (", "edit_entry": True})
        assert res.status_code == 422
        assert entry.read_text(encoding="utf-8") == before
        edited = '"""kit"""\ndef kit_a(x: int) -> int:\n    """改"""\n    return x + 1\n'
        assert client.post("/api/tools/custom", json={"name": "kit", "code": edited,
                                                       "edit_entry": True}).status_code == 200
        assert entry.read_text(encoding="utf-8") == edited
        assert sorted(p.name for p in entry.parent.iterdir()) == ["tools.py"]

    def test_reuploading_a_toolkit_replaces_its_folder_whole(self, studio_data):
        import io
        import zipfile
        from backend.api import custom_tools
        client = _client()

        def upload(files):
            blob = io.BytesIO()
            with zipfile.ZipFile(blob, "w") as archive:
                for name, text in files.items():
                    archive.writestr(f"kit/{name}", text)
            return client.post("/api/tools/custom/upload",
                               files={"file": ("kit.zip", blob.getvalue(), "application/zip")})
        code = '"""kit"""\ndef kit_a(x: int) -> int:\n    """a"""\n    return x\n'
        assert upload({"tools.py": code, "old.txt": "x"}).status_code == 200
        assert upload({"tools.py": code, "new.txt": "y"}).status_code == 200
        assert sorted(p.name for p in custom_tools.package_dir("kit").iterdir()) == ["new.txt", "tools.py"]
        assert sorted(p.name for p in custom_tools.TOOLS_DIR.iterdir()) == ["kit", "kit.json"]


class TestSkills:
    def test_round_trip_and_a_description_that_would_not_read_back(self, studio_data):
        from backend.api import skills_api
        client = _client()
        body = {"name": "rubric", "description": '描述: "引号" #hash', "content": "# 标题\n---\n内容 ✓"}
        assert client.post("/api/skills", json=body).status_code == 200
        got = client.get("/api/skills/rubric").json()
        assert (got["description"], got["content"]) == (body["description"], body["content"])
        before = (skills_api.SKILLS_DIR / "rubric" / "SKILL.md").read_text(encoding="utf-8")

        res = client.post("/api/skills", json={**body, "description": "a\n---\nb"})
        assert res.status_code == 422, res.text
        assert (skills_api.SKILLS_DIR / "rubric" / "SKILL.md").read_text(encoding="utf-8") == before
        assert client.get("/api/skills/rubric").status_code == 200
        assert not list((skills_api.SKILLS_DIR / "rubric").glob(".SKILL.*"))

    def test_wrong_types_are_422(self, studio_data):
        client = _client()
        for body in ({"name": 5, "description": "d", "content": "c"},
                     {"name": "ok", "description": 5, "content": "c"},
                     {"name": "ok", "description": "d", "content": ["c"]}):
            assert client.post("/api/skills", json=body).status_code == 422

    def test_an_overwrite_keeps_the_previous_version(self, studio_data):
        from backend.api import skills_api
        client = _client()
        for content in ("one", "two"):
            client.post("/api/skills", json={"name": "s", "description": "d", "content": content})
        versions = list((skills_api.SKILLS_DIR / "s" / ".versions").iterdir())
        assert len(versions) == 1 and "one" in versions[0].read_text(encoding="utf-8")


class TestEvaluatorDrafts:
    def test_concurrent_draft_saves_never_fail(self, studio_data):
        from backend.api import evaluator_tools
        gid = graphs.create_graph("Drafts", "")["id"]
        errors = []

        def save(i):
            try:
                evaluator_tools.put_draft(gid, f"# {i}\n" + "x" * 50000)
            except Exception as exc:          # noqa: BLE001 - collected for the assert
                errors.append(exc)
        threads = [threading.Thread(target=save, args=(i,)) for i in range(20)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert errors == []
        assert evaluator_tools.get_draft(gid)["code"].startswith("# ")
        assert [p.name for p in evaluator_tools.DRAFTS_DIR.iterdir()] == [f"{gid}.json"]

    def test_a_save_keeps_a_draft_typed_after_it(self, studio_data, monkeypatch):
        from backend.api import evaluator_tools
        gid = graphs.create_graph("Drafts", "")["id"]
        evaluator_tools.put_draft(gid, "saved code")
        read = evaluator_tools.get_draft
        calls = []

        def typed_meanwhile(graph_id):
            draft = read(graph_id)
            if not calls:
                calls.append(1)
                # The user keeps typing between the save's check and its clear.
                evaluator_tools.put_draft(graph_id, "newer code")
            return draft
        monkeypatch.setattr(evaluator_tools, "get_draft", typed_meanwhile)
        evaluator_tools.clear_saved_drafts(gid, [{"code": "saved code"}])
        assert read(gid)["code"] == "newer code"

    def test_a_save_clears_the_draft_it_saved(self, studio_data):
        client = _client()
        gid = graphs.create_graph("Drafts", "")["id"]
        code = 'def evaluate(records, 阈值: float = 0.5):\n    return {"metrics": {"n": len(records)}}\n'
        assert client.put(f"/api/graphs/{gid}/evaluators/draft", json={"code": code}).status_code == 200
        res = client.put(f"/api/graphs/{gid}/evaluators", json={"evaluators": [{"name": "评估", "code": code}]})
        assert res.status_code == 200, res.text
        assert client.get(f"/api/graphs/{gid}/evaluators/draft").json() == {"draft": None}
        bad = client.put(f"/api/graphs/{gid}/evaluators", json={"evaluators": [{"name": "x", "code": "def ("}]})
        assert bad.status_code == 422
        assert [e["name"] for e in client.get(f"/api/graphs/{gid}/evaluators").json()["evaluators"]] == ["评估"]


class TestScheduleValidation:
    @pytest.mark.parametrize("body", [{"enabled": "false"}, {"mode": 5}, {"time": 9},
                                      {"session": 5}, {"time_input": [1]}])
    def test_wrong_types_are_refused(self, body):
        from backend.api import scheduler
        with pytest.raises(scheduler.ScheduleError):
            scheduler.validate({"mode": "daily", **body})

    def test_enabled_false_stays_off(self):
        from backend.api import scheduler
        assert scheduler.validate({"enabled": False})["enabled"] is False


class TestWorkspaceWrites:
    def test_round_trip_and_atomic_replace(self, studio_data, monkeypatch):
        from backend.api import workspace
        gid = graphs.create_graph("Files", "")["id"]
        client = _client()
        text = '中文 ✓ "q" \\ \r\n\tend'
        assert client.put(f"/api/graphs/{gid}/workspace/file",
                          json={"path": "files/笔记 #1.txt", "content": text}).status_code == 200
        assert client.get(f"/api/graphs/{gid}/workspace/file",
                          params={"path": "files/笔记 #1.txt"}).json()["content"] == text

        def broken(source, destination):
            raise OSError("disk full")
        monkeypatch.setattr(workspace, "replace", broken)
        with pytest.raises(OSError):
            workspace.write_file(gid, "files/笔记 #1.txt", b"half")
        folder = workspace.workspace_root(gid) / "files"
        assert (folder / "笔记 #1.txt").read_text(encoding="utf-8", errors="strict").replace("\r\n", "\n") \
            == text.replace("\r\n", "\n")
        assert [p.name for p in folder.iterdir()] == ["笔记 #1.txt"]

    def test_bad_paths_and_types_are_4xx(self, studio_data):
        gid = graphs.create_graph("Files", "")["id"]
        client = _client()
        base = f"/api/graphs/{gid}/workspace"
        assert client.put(base + "/file", json={"path": "files/a.txt", "content": "a"}).status_code == 200
        for body in ({"path": "files/a.txt", "content": 5}, {"path": 5, "content": "a"}):
            assert client.put(base + "/file", json=body).status_code == 422
        assert client.put(base + "/file", json={"path": "files", "content": "a"}).status_code == 400
        assert client.put(base + "/file", json={"path": "files/a.txt/b.txt", "content": "a"}).status_code == 400
        assert client.post(base + "/mkdir", json={"path": "files/a.txt"}).status_code == 400
        assert client.post(base + "/mkdir", json={"path": 5}).status_code == 422

    def test_binary_upload_round_trip(self, studio_data):
        gid = graphs.create_graph("Files", "")["id"]
        client = _client()
        blob = bytes(range(256)) * 100
        base = f"/api/graphs/{gid}/workspace"
        assert client.post(base + "/upload", files={"file": ("二进制.bin", blob)}).status_code == 200
        assert client.get(base + "/download", params={"path": "files/二进制.bin"}).content == blob


class TestProjects:
    def test_round_trip_and_an_unreadable_file_is_skipped(self, studio_data, monkeypatch):
        from backend.api import projects
        monkeypatch.setattr(projects, "PROJECTS_DIR", studio_data / "projects")
        client = _client()
        created = client.post("/api/projects", json={"name": " 项目 <&> ", "description": "描述"}).json()
        assert created["name"] == "项目 <&>"
        updated = client.put(f"/api/projects/{created['id']}", json={"name": "改名 ✓"})
        assert updated.status_code == 200
        assert client.put(f"/api/projects/{created['id']}", json={"name": "  "}).status_code == 422
        (projects.PROJECTS_DIR / ("f" * 32 + ".json")).write_text("{broken", encoding="utf-8")
        (projects.PROJECTS_DIR / ("e" * 32 + ".json")).write_text('{"id": "x"}', encoding="utf-8")
        listed = client.get("/api/projects")
        assert listed.status_code == 200
        assert "改名 ✓" in [p.get("name") for p in listed.json()]
        assert not list(projects.PROJECTS_DIR.glob("*.tmp"))


class TestDataStores:
    def test_one_broken_file_does_not_fail_listings(self, studio_data, monkeypatch):
        from backend.api import studio_config
        from backend.features.data import data_resources, user_datasets
        monkeypatch.setattr(studio_config, "DATA_DIR", studio_data)
        graphs.GRAPHS_DIR.mkdir(parents=True, exist_ok=True)
        (graphs.GRAPHS_DIR / "broken.json").write_text("{nope", encoding="utf-8")
        (data_resources.directory() / ("a" * 32)).mkdir()
        (data_resources.directory() / ("a" * 32) / "manifest.json").write_text("{nope", encoding="utf-8")
        client = _client()
        csv = "名字,值\n行,1\n".encode()
        made = client.post("/api/datasets", files={"file": ("数据.csv", csv, "text/csv")})
        assert made.status_code == 200, made.text
        renamed = client.patch(f"/api/datasets/{made.json()['id']}", json={"name": "改名 ✓"})
        assert renamed.status_code == 200 and renamed.json()["name"] == "改名 ✓"
        assert client.get("/api/data-resources").status_code == 200

    def test_mem0_spaces_skip_an_unreadable_file(self, tmp_path, monkeypatch):
        from backend.api import mem0_service as service
        monkeypatch.setattr(service, "ROOT", tmp_path / "mem0")
        graph = {"id": "a", "project_id": "p"}
        made = service.create_space(graph, "空间 ✓")
        (service.ROOT / "spaces" / ("b" * 32 + ".json")).write_text("{nope", encoding="utf-8")
        assert [s["name"] for s in service.spaces(graph)] == ["空间 ✓"]
        assert json.loads((service.ROOT / "spaces" / f"{made['id']}.json").read_text(encoding="utf-8"))["name"] == "空间 ✓"
