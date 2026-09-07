"""Short-term memory: what happened earlier in this session.

The long-term store is a vector index searched by similarity, and it has no
sense of order or of "this time round" — asking it a question returns the three
most similar things the node ever saw. That is the wrong shape for the other
kind of remembering, so the two are kept apart: a session log, read back in
order, alongside the store, searched by likeness.
"""

import json

import pytest


@pytest.fixture
def stm(tmp_path, monkeypatch):
    import stm_store

    monkeypatch.setattr(stm_store, "STM_DIR", tmp_path / "stm")
    return stm_store


def entry(node, **outputs):
    return {"node": node, "run_id": "r1", "at": "2026-09-01",
            "inputs": {}, "outputs": outputs}


class TestTheSessionLog:
    def test_entries_come_back_in_the_order_they_happened(self, stm):
        for i in range(3):
            stm.append("g", "s", entry("note", step=i))
        assert [e["outputs"]["step"] for e in stm.recent("g", "s", 5)] == [0, 1, 2]

    def test_only_the_last_few(self, stm):
        for i in range(6):
            stm.append("g", "s", entry("note", step=i))
        assert [e["outputs"]["step"] for e in stm.recent("g", "s", 2)] == [4, 5]

    def test_sessions_do_not_see_each_other(self, stm):
        stm.append("g", "morning", entry("note", step="a"))
        stm.append("g", "evening", entry("note", step="b"))
        assert [e["outputs"]["step"] for e in stm.recent("g", "morning", 5)] == ["a"]
        assert sorted(stm.sessions("g")) == ["evening", "morning"]

    def test_workflows_do_not_see_each_other(self, stm):
        stm.append("mine", "s", entry("note", step="a"))
        assert stm.recent("theirs", "s", 5) == []

    def test_nothing_is_recorded_without_a_session(self, stm):
        # Two records of a batch are usually independent; letting one
        # contaminate the next by default would be worse than forgetting.
        stm.append("g", "", entry("note", step="a"))
        assert stm.sessions("g") == []

    def test_a_session_does_not_grow_without_bound(self, stm):
        for i in range(stm.MAX_PER_SESSION + 20):
            stm.append("g", "s", entry("note", step=i))
        kept = stm.recent("g", "s", stm.MAX_PER_SESSION * 2)

        assert len(kept) == stm.MAX_PER_SESSION
        # The oldest go first: a log is most useful at its recent end.
        assert kept[-1]["outputs"]["step"] == stm.MAX_PER_SESSION + 19

    def test_it_survives_the_process(self, stm):
        stm.append("g", "s", entry("note", step="a"))
        # A new store object, as a later run would build.
        assert stm.open_store("g").get("s")[0]["outputs"]["step"] == "a"

    def test_a_workflow_with_no_log_has_no_sessions(self, stm):
        assert stm.sessions("never-run") == []

    def test_clearing_one_session_leaves_the_others(self, stm):
        stm.append("g", "a", entry("note"))
        stm.append("g", "b", entry("note"))
        stm.clear("g", "a")
        assert stm.sessions("g") == ["b"]


class TestWhatTheNodeIsTold:
    @staticmethod
    def task(**memory):
        return {"name": "note", "inputs": [{"name": "company"}],
                "outputs": [{"name": "fact"}],
                **({"memory": memory} if memory else {})}

    def test_it_reads_as_a_sequence_not_a_search(self):
        import memory_policy

        entries = [
            {"node": "note", "inputs": {"company": "A"}, "outputs": {"fact": "one"}},
            {"node": "note", "inputs": {"company": "B"}, "outputs": {"fact": "two"}},
        ]
        block = memory_policy.session_block(entries, self.task())

        assert "Earlier in this session:" in block
        assert block.index("one") < block.index("two")

    def test_another_node_s_step_says_whose_it_was(self):
        import memory_policy

        entries = [{"node": "investigate", "inputs": {}, "outputs": {"context": "c"}},
                   {"node": "note", "inputs": {}, "outputs": {"fact": "f"}}]
        block = memory_policy.session_block(entries, self.task())

        assert "- (investigate) context: c" in block
        assert "- fact: f" in block

    def test_the_node_says_how_much_of_the_session_it_wants(self):
        import memory_policy

        entries = [{"node": "note", "inputs": {}, "outputs": {"fact": str(i)}}
                   for i in range(5)]
        block = memory_policy.session_block(entries, self.task(session_recall=2))

        assert block.count("- ") == 2
        assert "fact: 4" in block and "fact: 0" not in block

    def test_a_node_can_opt_out_of_it_entirely(self):
        import memory_policy

        entries = [{"node": "note", "inputs": {}, "outputs": {"fact": "f"}}]
        assert memory_policy.session_block(entries, self.task(session_recall=0)) == ""

    def test_the_read_selection_applies_here_too(self):
        import memory_policy

        entries = [{"node": "note", "inputs": {"company": "A"},
                    "outputs": {"fact": "f"}}]
        block = memory_policy.session_block(entries, self.task(read=["fact"]))
        assert "fact: f" in block and "company" not in block

    def test_an_empty_session_adds_nothing(self):
        import memory_policy

        assert memory_policy.session_block([], self.task()) == ""

    def test_the_default_is_on_but_only_matters_with_a_session(self):
        import memory_policy

        # Nothing changes for a node set up before this existed: no run
        # supplies a session unless someone asks for one.
        assert memory_policy.policy(self.task())["session_recall"] \
            == memory_policy.DEFAULT_SESSION_RECALL


class TestThroughTheRunner:
    @staticmethod
    def doc(**memory):
        return {"id": "g1", "goal": "g", "edges": [], "tasks": [{
            "name": "note", "description": "note", "prompt": "Company: {company}",
            "use_long_term_memory": True,
            "inputs": [{"name": "company"}], "outputs": [{"name": "fact"}],
            **({"memory": memory} if memory else {}),
        }]}

    def test_a_run_records_what_it_kept_into_its_session(self, stm, monkeypatch):
        import runner

        class Param:
            def __init__(self, name): self.name = name

        class Node:
            name = "note"
            inputs = [Param("company")]
            outputs = [Param("fact")]

        class Graph:
            nodes = [Node()]

        class Workflow:
            def __init__(self): self.environment = self
            def get_all_execution_data(self):
                return {"company": "Gaucho", "fact": "a fact"}

        class Memory:
            storage_handler = object()
            def add(self, messages): pass
            def save(self): pass

        state = {"run_id": "r1", "session": "morning"}
        runner._save_ltm(self.doc(), {"note": Memory()}, Graph(), Workflow(), state)

        [recorded] = stm.recent("g1", "morning", 5)
        assert recorded["node"] == "note"
        assert recorded["outputs"] == {"fact": "a fact"}
        assert recorded["run_id"] == "r1"

    def test_a_run_with_no_session_records_nothing(self, stm):
        import runner

        class Node:
            name = "note"
            inputs = []
            outputs = []

        class Graph:
            nodes = [Node()]

        class Workflow:
            def __init__(self): self.environment = self
            def get_all_execution_data(self): return {"fact": "a fact"}

        class Memory:
            storage_handler = object()
            def add(self, messages): pass
            def save(self): pass

        runner._save_ltm(self.doc(), {"note": Memory()}, Graph(), Workflow(),
                         {"run_id": "r1"})
        assert stm.sessions("g1") == []

    def test_the_session_is_kept_on_the_run(self, stm, monkeypatch):
        import runner

        monkeypatch.setattr(runner, "RUNS_DIR", stm.STM_DIR.parent / "runs")
        runner._runs.clear()
        run_id = runner.start_run({"id": "g1", "tasks": []}, {},
                                  background=False, session="  morning  ")
        # Trimmed, and absent rather than empty when it was not given.
        assert runner.get_run(run_id)["session"] == "morning"

        plain = runner.start_run({"id": "g1", "tasks": []}, {}, background=False)
        assert runner.get_run(plain)["session"] is None


class TestInTheWorkspace:
    @pytest.fixture
    def shown(self, stm, tmp_path, monkeypatch):
        import memory_store
        import workspace

        monkeypatch.setattr(workspace, "WORKSPACE_DIR", tmp_path / "workspace")
        monkeypatch.setattr(memory_store, "list_agents", lambda gid: ["note"])
        monkeypatch.setattr(memory_store, "list_entries", lambda gid, agent: [
            {"memory_id": "x", "timestamp": "2026-09-01",
             "content": json.dumps({"outputs": {"fact": "remembered"}})}])
        stm.append("g1", "morning", entry("note", fact="just now"))
        return workspace

    def test_the_two_kinds_are_named_apart(self, shown):
        paths = [e["path"] for e in shown.tree("g1")]

        # "Which is short and which is long" should not need explaining.
        assert "memory/long-term/note" in paths
        assert "memory/short-term/morning" in paths

    def test_a_session_entry_reads_back(self, shown):
        content = json.loads(
            shown.read_file("g1", "memory/short-term/morning/001.json")["content"])
        assert content["outputs"] == {"fact": "just now"}

    def test_session_entries_are_in_order_not_newest_first(self, shown, stm):
        stm.append("g1", "morning", entry("note", fact="later"))
        first = json.loads(
            shown.read_file("g1", "memory/short-term/morning/001.json")["content"])
        # The long-term view is newest-first because you want the latest; a
        # session is a sequence, so it reads forwards.
        assert first["outputs"]["fact"] == "just now"

    def test_it_is_read_only_like_the_rest_of_memory(self, shown):
        assert shown.read_file("g1", "memory/short-term/morning/001.json")["readonly"]
        with pytest.raises(shown.WorkspaceError):
            shown.delete_file("g1", "memory/short-term/morning/001.json")

    def test_a_session_named_awkwardly_cannot_escape(self, shown, stm):
        stm.append("g1", "../../escape", entry("note"))
        paths = [e["path"] for e in shown.tree("g1")]
        assert all(not p.startswith("..") for p in paths)
        assert any("escape" in p for p in paths)
