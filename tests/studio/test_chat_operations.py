"""Tests for the chat operation engine (studio/backend/chat_api.py).

The engine applies a model's proposed edits to a canvas graph. It is the one
place where an LLM's output mutates the user's workflow, so the behaviours that
matter are the defensive ones: a malformed operation must not discard the good
ones, a rename must not orphan edges, and the caller's graph must come back
untouched when something goes wrong.

These cover the bugs found while building the feature:

1. `applied` was true whenever any operation ran, so a turn of pure reads
   created a bogus undo entry on the client.
2. Nodes added with a name already on the canvas silently collided.
3. `generate_workflow` mixed with other operations replaced the canvas and
   discarded them without saying so.
"""

from conftest import make_graph, make_task


def apply(graph, operations, graph_id=None):
    import chat_api

    return chat_api.apply_operations(graph, operations, graph_id)


class TestNodeEditing:
    def test_add_node_slugifies_and_deduplicates(self):
        graph = make_graph([make_task("a", outputs=["x"])])
        result = apply(graph, [
            {"op": "add_node", "task": make_task("C Node!", outputs=["z"])},
            {"op": "add_node", "task": make_task("a", outputs=["y"])},
        ])
        names = [t["name"] for t in result["graph"]["tasks"]]
        assert names == ["a", "c_node", "a_2"]
        assert any("already taken" in note for note in result["notes"])

    def test_update_node_merges_and_keeps_other_fields(self):
        graph = make_graph([make_task("a", inputs=["q"], outputs=["x"])])
        result = apply(graph, [
            {"op": "update_node", "name": "a", "patch": {"parse_mode": "json"}},
        ])
        task = result["graph"]["tasks"][0]
        assert task["parse_mode"] == "json"
        assert [i["name"] for i in task["inputs"]] == ["q"]

    def test_update_node_cannot_rename(self):
        graph = make_graph([make_task("a", outputs=["x"])])
        result = apply(graph, [
            {"op": "update_node", "name": "a", "patch": {"name": "b", "parse_mode": "json"}},
        ])
        assert result["graph"]["tasks"][0]["name"] == "a"
        assert result["graph"]["tasks"][0]["parse_mode"] == "json"

    def test_rename_rewires_edges(self):
        graph = make_graph(
            [make_task("a", outputs=["x"]), make_task("b", inputs=["x"], outputs=["y"])],
            edges=[("a", "b")],
        )
        result = apply(graph, [{"op": "rename_node", "name": "b", "new_name": "middle"}])
        assert [t["name"] for t in result["graph"]["tasks"]] == ["a", "middle"]
        assert result["graph"]["edges"] == [{"source": "a", "target": "middle"}]

    def test_delete_node_removes_its_edges(self):
        graph = make_graph(
            [make_task("a", outputs=["x"]), make_task("b", inputs=["x"], outputs=["y"])],
            edges=[("a", "b")],
        )
        result = apply(graph, [{"op": "delete_node", "name": "b"}])
        assert [t["name"] for t in result["graph"]["tasks"]] == ["a"]
        assert result["graph"]["edges"] == []


class TestFailureIsolation:
    def test_one_bad_operation_does_not_lose_the_others(self):
        graph = make_graph([make_task("a", outputs=["x"])])
        result = apply(graph, [
            {"op": "add_node", "task": make_task("b", inputs=["x"], outputs=["y"])},
            {"op": "add_edge", "source": "ghost", "target": "a"},
            {"op": "frobnicate"},
            {"op": "add_edge", "source": "a", "target": "b"},
        ])
        assert [t["name"] for t in result["graph"]["tasks"]] == ["a", "b"]
        assert result["graph"]["edges"] == [{"source": "a", "target": "b"}]
        assert len(result["notes"]) == 2

    def test_unknown_operation_is_reported_not_raised(self):
        result = apply(make_graph([]), [{"op": "nope"}])
        assert any("nope" in note for note in result["notes"])

    def test_non_object_operation_is_skipped(self):
        result = apply(make_graph([]), ["not an operation"])
        assert result["notes"]

    def test_caller_graph_is_never_mutated(self):
        graph = make_graph([make_task("a", outputs=["x"])], edges=[])
        before = repr(graph)
        apply(graph, [
            {"op": "add_node", "task": make_task("b", outputs=["y"])},
            {"op": "delete_node", "name": "a"},
        ])
        assert repr(graph) == before

    def test_self_edge_is_refused(self):
        graph = make_graph([make_task("a", outputs=["x"])])
        result = apply(graph, [{"op": "add_edge", "source": "a", "target": "a"}])
        assert result["graph"]["edges"] == []
        assert any("itself" in note for note in result["notes"])

    def test_duplicate_edge_is_not_added_twice(self):
        graph = make_graph(
            [make_task("a", outputs=["x"]), make_task("b", inputs=["x"], outputs=["y"])],
            edges=[("a", "b")],
        )
        result = apply(graph, [{"op": "add_edge", "source": "a", "target": "b"}])
        assert result["graph"]["edges"] == [{"source": "a", "target": "b"}]


class TestSideEffectingOperations:
    def test_run_workflow_only_proposes(self, studio_data):
        """A run costs money and its tools touch the world: the user confirms."""
        import graphs as graph_store

        created = graph_store.create_graph("Runnable", "goal")
        graph = make_graph([make_task("a", inputs=["topic"], outputs=["x"])],
                           id=created["id"])
        result = apply(graph, [{"op": "run_workflow", "inputs": {"topic": "t"}}],
                       created["id"])
        assert result["pending_run"] == {"inputs": {"topic": "t"}}
        assert result["saved"] is False

    def test_save_graph_refuses_an_invalid_workflow(self, studio_data):
        import graphs as graph_store

        created = graph_store.create_graph("Broken", "goal")
        # Referencing a skill that does not exist fails validation.
        task = make_task("a", inputs=["topic"], outputs=["x"], skill_names=["ghost"])
        result = apply(make_graph([task], id=created["id"]),
                       [{"op": "save_graph"}], created["id"])
        assert result["saved"] is False
        assert any("save_graph refused" in note for note in result["notes"])

    def test_create_skill_then_delete(self, studio_data):
        import skills_api

        result = apply(make_graph([]), [
            {"op": "create_skill", "spec": {"name": "tone", "description": "d",
                                            "content": "# Tone"}},
        ])
        assert result["skills_created"] == ["tone"]
        assert skills_api.get_skill("tone")["content"] == "# Tone"

        result = apply(make_graph([]), [{"op": "delete_skill", "name": "tone"}])
        assert skills_api.get_skill("tone") is None

    def test_delete_missing_skill_is_a_note(self, studio_data):
        result = apply(make_graph([]), [{"op": "delete_skill", "name": "ghost"}])
        assert any("ghost" in note for note in result["notes"])


class TestReadOperations:
    def test_validate_reports_errors_without_changing_the_graph(self, studio_data):
        task = make_task("a", inputs=["topic"], outputs=["x"], skill_names=["ghost"])
        graph = make_graph([task])
        result = apply(graph, [{"op": "validate"}])
        observation = result["observations"][0]
        assert observation["op"] == "validate"
        assert observation["result"]["errors"]
        assert result["graph"]["tasks"] == graph["tasks"]

    def test_input_missing_from_the_prompt_still_validates(self, studio_data):
        """A known gap, pinned here so a future fix updates this test.

        The framework only rejects an input the prompt never references when
        the agent is built, i.e. during a run — after the user has paid for it.
        Save-time validation lets it through, which is why the Inspector warns
        about it in the editor instead.
        """
        task = make_task("a", inputs=["topic"], outputs=["x"], prompt="no placeholder")
        result = apply(make_graph([task]), [{"op": "validate"}])
        assert result["observations"][0]["result"]["errors"] == []

    def test_validate_lists_workflow_inputs_when_sound(self, studio_data):
        graph = make_graph([make_task("a", inputs=["topic"], outputs=["x"])])
        result = apply(graph, [{"op": "validate"}])
        found = result["observations"][0]["result"]
        assert found["errors"] == []
        assert [i["name"] for i in found["workflow_inputs"]] == ["topic"]

    def test_inspect_node_returns_the_stored_task(self):
        graph = make_graph([make_task("a", outputs=["x"])])
        result = apply(graph, [{"op": "inspect_node", "name": "a"}])
        assert result["observations"][0]["result"]["name"] == "a"

    def test_inspect_missing_node_is_a_note(self):
        result = apply(make_graph([]), [{"op": "inspect_node", "name": "ghost"}])
        assert not result["observations"]
        assert any("ghost" in note for note in result["notes"])
