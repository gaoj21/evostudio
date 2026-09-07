"""Tests for canvas graph validation and derived state (studio/backend/graphs.py).

`validate_graph` is what stands between a canvas and a run that costs money, and
`compute_workflow_inputs` decides what the run dialog asks for. Both are pure,
so they are cheap to pin down.

Covers the behaviours the UI depends on:

1. An input no upstream node produces becomes a workflow input — that is what
   the Run dialog collects, and `consumed_by` tells it which node needs it.
2. A source node that is not wired to anything is skipped at run time, so its
   outputs must not count as produced.
3. Skills are canvas-only: they never reach the framework, but a reference to
   one that no longer exists has to fail at save time rather than mid-run.
"""

import pytest

from conftest import make_graph, make_task


def inputs_of(graph):
    import graphs as graph_store

    ordered = graph_store.topo_sort_tasks(graph["tasks"], graph["edges"])
    return graph_store.compute_workflow_inputs(ordered, graph["edges"])


class TestWorkflowInputs:
    def test_unmatched_input_becomes_a_workflow_input(self):
        graph = make_graph([make_task("a", inputs=["topic"], outputs=["x"])])
        assert [i["name"] for i in inputs_of(graph)] == ["topic"]

    def test_upstream_output_satisfies_a_downstream_input(self):
        graph = make_graph(
            [make_task("a", outputs=["x"]), make_task("b", inputs=["x"], outputs=["y"])],
            edges=[("a", "b")],
        )
        assert inputs_of(graph) == []

    def test_input_carries_the_node_that_consumes_it(self):
        graph = make_graph([make_task("detect", inputs=["news"], outputs=["finding"])])
        assert inputs_of(graph)[0]["consumed_by"] == "detect"

    def test_unwired_source_node_outputs_are_still_asked_for(self):
        """An unconnected source is skipped at run time, so it produces nothing."""
        source = {"name": "feed", "kind": "source", "source": {"type": "credit_risk"},
                  "outputs": [{"name": "company", "type": "str",
                               "description": "c", "required": True}]}
        graph = make_graph([source, make_task("a", inputs=["company"], outputs=["x"])])
        assert [i["name"] for i in inputs_of(graph)] == ["company"]

    def test_wired_source_node_covers_the_input(self):
        source = {"name": "feed", "kind": "source", "source": {"type": "credit_risk"},
                  "outputs": [{"name": "company", "type": "str",
                               "description": "c", "required": True}]}
        graph = make_graph([source, make_task("a", inputs=["company"], outputs=["x"])],
                           edges=[("feed", "a")])
        assert inputs_of(graph) == []

    def test_parked_node_inputs_are_not_asked_for(self):
        """A node with no edges at all is an inert draft."""
        graph = make_graph(
            [make_task("a", inputs=["topic"], outputs=["x"]),
             make_task("b", inputs=["y"], outputs=["y"]),
             make_task("draft", inputs=["nobody_supplies_this"], outputs=["z"])],
            edges=[("a", "b")],
        )
        names = [i["name"] for i in inputs_of(graph)]
        assert "nobody_supplies_this" not in names


class TestValidation:
    def test_a_sound_graph_validates(self, studio_data):
        import graphs as graph_store

        graph = make_graph(
            [make_task("a", outputs=["x"]), make_task("b", inputs=["x"], outputs=["y"])],
            edges=[("a", "b")],
        )
        ordered, workflow_inputs = graph_store.validate_graph(graph)
        assert [t["name"] for t in ordered] == ["a", "b"]
        assert workflow_inputs == []

    def test_unknown_skill_is_rejected(self, studio_data):
        import graphs as graph_store

        graph = make_graph([make_task("a", outputs=["x"], skill_names=["ghost"])])
        with pytest.raises(graph_store.GraphValidationError) as raised:
            graph_store.validate_graph(graph)
        assert "ghost" in str(raised.value)

    def test_a_known_skill_passes_and_never_reaches_the_framework(self, studio_data):
        import graphs as graph_store
        import skills_api

        skills_api.save_skill({"name": "rubric", "description": "d", "content": "# R"})
        task = make_task("a", outputs=["x"], skill_names=["rubric"])
        graph_store.validate_graph(make_graph([task]))
        assert "skill_names" not in graph_store.strip_task(task)

    def test_tool_node_fed_by_an_llm_node_is_rejected(self, studio_data):
        """Tool nodes run before the graph, so they cannot consume its output."""
        import graphs as graph_store

        llm = make_task("a", outputs=["x"])
        tool = {"name": "t", "kind": "tool", "tool": "word_count",
                "inputs": [{"name": "x", "type": "str", "description": "x",
                            "required": True}],
                "outputs": [{"name": "count", "type": "str", "description": "c",
                             "required": True}]}
        graph = make_graph([llm, tool], edges=[("a", "t")])
        with pytest.raises(graph_store.GraphValidationError):
            graph_store.validate_graph(graph)


class TestTopologyHelpers:
    def test_topo_sort_follows_the_edges(self):
        import graphs as graph_store

        graph = make_graph(
            [make_task("c", inputs=["y"], outputs=["z"]),
             make_task("a", outputs=["x"]),
             make_task("b", inputs=["x"], outputs=["y"])],
            edges=[("a", "b"), ("b", "c")],
        )
        ordered = graph_store.topo_sort_tasks(graph["tasks"], graph["edges"])
        assert [t["name"] for t in ordered] == ["a", "b", "c"]

    def test_parked_nodes_are_the_ones_with_no_edges(self):
        import graphs as graph_store

        graph = make_graph(
            [make_task("a", outputs=["x"]), make_task("b", inputs=["x"], outputs=["y"]),
             make_task("lonely", outputs=["z"])],
            edges=[("a", "b")],
        )
        parked = graph_store.parked_task_names(graph["tasks"], graph["edges"])
        assert parked == {"lonely"}

    def test_auto_layout_columns_by_dependency_depth(self):
        import graphs as graph_store

        graph = make_graph(
            [make_task("a", outputs=["x"]), make_task("b", inputs=["x"], outputs=["y"]),
             make_task("c", inputs=["y"], outputs=["z"])],
            edges=[("a", "b"), ("b", "c")],
        )
        graph_store.auto_layout(graph)
        xs = {t["name"]: t["x"] for t in graph["tasks"]}
        assert xs["a"] < xs["b"] < xs["c"]

    def test_auto_layout_survives_a_cycle(self):
        """A cycle cannot be ordered by depth; it must not hang or raise."""
        import graphs as graph_store

        graph = make_graph(
            [make_task("a", inputs=["y"], outputs=["x"]),
             make_task("b", inputs=["x"], outputs=["y"])],
            edges=[("a", "b"), ("b", "a")],
        )
        graph_store.auto_layout(graph)
        assert all("x" in t and "y" in t for t in graph["tasks"])
