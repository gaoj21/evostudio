"""Reading a workflow's long-term memory where the rest of it lives.

Memory used to be reachable only through a tab inside the run drawer, so what
a workflow had learned was invisible unless a run happened to be open. It sits
in the workspace now, beside the code and the runs — but as a view of the
vector store rather than a copy, which would be wrong the moment the next run
wrote to it.
"""

import json

import pytest


@pytest.fixture
def remembering(studio_data, monkeypatch):
    """A workspace whose graph has two nodes with memory behind them."""
    import memory_store
    import workspace

    monkeypatch.setattr(workspace, "WORKSPACE_DIR", studio_data / "workspace")

    entries = {
        "decide": [
            {"memory_id": "a", "timestamp": "2026-09-01T10:00:00",
             "agent": "decide", "wf_task": "decide",
             "content": json.dumps({"task": "decide",
                                    "inputs": {"company": "Gaucho"},
                                    "outputs": {"decision": "suppress"}})},
            {"memory_id": "b", "timestamp": "2026-09-03T10:00:00",
             "agent": "decide", "wf_task": "decide",
             "content": json.dumps({"task": "decide",
                                    "inputs": {"company": "23andMe"},
                                    "outputs": {"decision": "alert"}})},
        ],
        "investigate": [
            {"memory_id": "c", "timestamp": "2026-09-02T10:00:00",
             "agent": "investigate", "wf_task": "investigate",
             "content": json.dumps({"outputs": {"context": "none"}})},
        ],
    }
    monkeypatch.setattr(memory_store, "list_agents", lambda gid: sorted(entries))
    monkeypatch.setattr(memory_store, "list_entries",
                        lambda gid, agent: list(entries[agent]))
    return workspace, entries


def paths(workspace, graph_id="probe"):
    return [e["path"] for e in workspace.tree(graph_id)]


class TestItIsInTheTree:
    def test_each_node_that_remembers_gets_a_folder(self, remembering):
        workspace, _ = remembering
        listed = paths(workspace)

        # Named, not merged: the two kinds answer different questions.
        assert "memory" in listed
        assert "memory/long-term" in listed
        assert "memory/long-term/decide" in listed
        assert "memory/long-term/investigate" in listed

    def test_one_file_per_thing_remembered(self, remembering):
        workspace, _ = remembering
        assert [p for p in paths(workspace) if p.startswith("memory/long-term/decide/")] \
            == ["memory/long-term/decide/001.json", "memory/long-term/decide/002.json"]

    def test_newest_first(self, remembering):
        workspace, _ = remembering
        first = json.loads(
            workspace.read_file("probe", "memory/long-term/decide/001.json")["content"])
        # What the workflow learned most recently is what you look at first.
        assert first["memory_id"] == "b"

    def test_it_shows_up_without_a_workspace_on_disk(self, remembering):
        workspace, _ = remembering
        # A workflow that has run but never been saved still has memory.
        assert not workspace.workspace_root("probe").exists()
        assert "memory/long-term/decide/001.json" in paths(workspace)

    def test_a_workflow_with_no_memory_has_no_folder(self, remembering, monkeypatch):
        import memory_store

        workspace, _ = remembering
        monkeypatch.setattr(memory_store, "list_agents", lambda gid: [])
        assert not any(p.startswith("memory") for p in paths(workspace))

    def test_a_store_that_will_not_open_does_not_break_the_listing(
        self, remembering, monkeypatch
    ):
        import memory_store

        workspace, _ = remembering
        monkeypatch.setattr(memory_store, "list_entries",
                            lambda gid, agent: (_ for _ in ()).throw(RuntimeError("bad index")))
        # The rest of the workspace is still worth listing.
        assert "memory/long-term/decide" in paths(workspace)


class TestReadingOne:
    def test_it_reads_back_as_the_entry(self, remembering):
        workspace, _ = remembering
        entry = json.loads(
            workspace.read_file("probe", "memory/long-term/decide/002.json")["content"])

        assert entry["memory_id"] == "a"
        assert entry["agent"] == "decide"

    def test_the_content_is_unwrapped(self, remembering):
        workspace, _ = remembering
        entry = json.loads(
            workspace.read_file("probe", "memory/long-term/decide/002.json")["content"])

        # Left as it comes out of the store this is a JSON string inside JSON,
        # which reads as a wall of escaped quotes.
        assert entry["content"]["outputs"] == {"decision": "suppress"}

    def test_it_carries_the_one_line_summary(self, remembering):
        workspace, _ = remembering
        entry = json.loads(
            workspace.read_file("probe", "memory/long-term/decide/002.json")["content"])
        assert entry["summary"] == "company: Gaucho → decision: suppress"

    def test_it_is_marked_read_only(self, remembering):
        workspace, _ = remembering
        # There is no file to edit, so the editor must not offer to.
        assert workspace.read_file("probe", "memory/long-term/decide/001.json")["readonly"]

    def test_an_entry_that_is_not_there(self, remembering):
        workspace, _ = remembering
        with pytest.raises(workspace.WorkspaceError) as raised:
            workspace.read_file("probe", "memory/long-term/decide/099.json")
        assert raised.value.not_found


class TestItIsAViewNotFiles:
    def test_writing_into_it_is_refused(self, remembering):
        workspace, _ = remembering
        with pytest.raises(workspace.WorkspaceError) as raised:
            workspace.write_file("probe", "memory/long-term/decide/003.json", b"{}")
        assert "view of this workflow's long-term memory" in str(raised.value)

    def test_deleting_from_it_is_refused(self, remembering):
        workspace, _ = remembering
        with pytest.raises(workspace.WorkspaceError) as raised:
            workspace.delete_file("probe", "memory/long-term/decide/001.json")
        # And it says where you *can* change what is kept.
        assert "Inspector" in str(raised.value)

    def test_deleting_the_whole_folder_is_refused(self, remembering):
        workspace, _ = remembering
        with pytest.raises(workspace.WorkspaceError):
            workspace.delete_file("probe", "memory", recursive=True)

    def test_a_real_file_called_memory_something_is_unaffected(self, remembering):
        workspace, _ = remembering
        # Only the `memory/` view is protected, not every path with the word.
        assert workspace.write_file("probe", "memories.txt", b"mine")


class TestDownloading:
    def test_one_entry(self, remembering):
        workspace, _ = remembering
        name, media, blob = workspace.download("probe", "memory/long-term/decide/001.json")

        assert media == "application/json"
        assert "decide" in name
        assert json.loads(blob)["memory_id"] == "b"

    def test_one_node_s_memory(self, remembering):
        import io
        import zipfile

        workspace, _ = remembering
        name, media, blob = workspace.download("probe", "memory/long-term/decide")

        assert name == "decide.zip" and media == "application/zip"
        assert zipfile.ZipFile(io.BytesIO(blob)).namelist() == [
            "decide/001.json", "decide/002.json"]

    def test_all_of_it(self, remembering):
        import io
        import zipfile

        workspace, _ = remembering
        name, _media, blob = workspace.download("probe", "memory")
        inside = zipfile.ZipFile(io.BytesIO(blob)).namelist()

        assert name == "memory.zip"
        assert "memory/long-term/decide/001.json" in inside
        assert "memory/long-term/investigate/001.json" in inside

    def test_an_empty_memory_path(self, remembering, monkeypatch):
        import memory_store

        workspace, _ = remembering
        monkeypatch.setattr(memory_store, "list_agents", lambda gid: [])
        with pytest.raises(workspace.WorkspaceError) as raised:
            workspace.download("probe", "memory/long-term/decide")
        assert raised.value.not_found
