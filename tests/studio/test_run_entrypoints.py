"""Tests for the two ways a run can be shaped before it starts.

**Starting part-way through** drops the nodes before a chosen one, which turns
whatever they would have produced into inputs the caller supplies — normally
the outputs of an earlier run.

**Preprocessing** puts every input record through a custom tool first, for a
batch and a single run alike, so cleaning or deriving fields happens once and
travels with the workflow.

Pins the bug found while building this: the prefill took the most recent run
holding *any* of the wanted values, which is typically the failed run you are
re-running — it never produced the later ones, so the re-run died on a missing
input instead of using the good run just behind it.
"""

import pytest

from conftest import make_graph, make_task


def chain():
    """a -> b -> c, each consuming what the one before produced."""
    return make_graph(
        [
            make_task("a", inputs=["topic"], outputs=["x"]),
            make_task("b", inputs=["x"], outputs=["y"]),
            make_task("c", inputs=["y"], outputs=["z"]),
        ],
        edges=[("a", "b"), ("b", "c")],
    )


def join_graph():
    """Two producers feeding one node, so a shortened run needs both values.

    A single-input shape cannot show the prefill bug: a run holding none of the
    wanted keys is skipped by any rule. It takes a node needing two values, and
    a recent run that produced only one of them.
    """
    return make_graph(
        [
            make_task("a", inputs=["topic"], outputs=["x"]),
            make_task("b", inputs=["topic"], outputs=["y"]),
            make_task("join", inputs=["x", "y"], outputs=["z"]),
        ],
        edges=[("a", "join"), ("b", "join")],
    )


class TestSubgraphFrom:
    def _cut(self, graph, start_at):
        import graphs as graph_store

        return graph_store.subgraph_from(graph["tasks"], graph["edges"], start_at)

    def test_no_start_returns_everything(self):
        graph = chain()
        tasks, edges = self._cut(graph, [])
        assert tasks is graph["tasks"] and edges is graph["edges"]

    def test_start_node_is_included_with_its_descendants(self):
        tasks, edges = self._cut(chain(), ["b"])
        assert [t["name"] for t in tasks] == ["b", "c"]
        assert edges == [{"source": "b", "target": "c"}]

    def test_edges_into_the_dropped_part_are_removed(self):
        tasks, edges = self._cut(chain(), ["c"])
        assert [t["name"] for t in tasks] == ["c"]
        assert edges == []

    def test_two_branches_both_follow(self):
        graph = make_graph(
            [make_task("root", outputs=["x"]),
             make_task("left", inputs=["x"], outputs=["l"]),
             make_task("right", inputs=["x"], outputs=["r"]),
             make_task("join", inputs=["l", "r"], outputs=["out"])],
            edges=[("root", "left"), ("root", "right"), ("left", "join"), ("right", "join")],
        )
        tasks, _ = self._cut(graph, ["root"])
        assert {t["name"] for t in tasks} == {"root", "left", "right", "join"}

        tasks, _ = self._cut(graph, ["left"])
        assert {t["name"] for t in tasks} == {"left", "join"}

    def test_several_start_nodes_union_their_reach(self):
        graph = make_graph(
            [make_task("a", outputs=["x"]), make_task("b", inputs=["x"], outputs=["y"]),
             make_task("solo", outputs=["s"])],
            edges=[("a", "b")],
        )
        tasks, _ = self._cut(graph, ["b", "solo"])
        assert {t["name"] for t in tasks} == {"b", "solo"}

    def test_unknown_name_alongside_a_real_one_is_ignored(self):
        tasks, _ = self._cut(chain(), ["b", "ghost"])
        assert [t["name"] for t in tasks] == ["b", "c"]

    def test_only_unknown_names_is_an_error(self):
        import graphs as graph_store

        with pytest.raises(graph_store.GraphValidationError):
            self._cut(chain(), ["ghost"])

    def test_upstream_outputs_become_inputs_of_the_shortened_run(self):
        import graphs as graph_store

        tasks, edges = self._cut(chain(), ["b"])
        ordered = graph_store.topo_sort_tasks(tasks, edges)
        wanted = graph_store.compute_workflow_inputs(ordered, edges)
        # `x` came from `a`, which is no longer part of the run.
        assert [i["name"] for i in wanted] == ["x"]
        assert wanted[0]["consumed_by"] == "b"


def save_tool(code):
    """Save the tool a piece of code defines.

    Nothing but the code: a tool is a documented function, so its name,
    description and parameters all come from it.
    """
    import custom_tools
    import tools_registry

    return custom_tools.save_custom_tool(
        custom_tools.validate_spec({"code": code}, tools_registry.builtin_names())
    )


class TestPreprocess:
    def test_no_preprocessor_passes_the_records_through(self, studio_data):
        import preprocess

        records = [{"a": 1}]
        assert preprocess.apply({}, records) is records
        assert preprocess.apply({"preprocess": "  "}, records) is records

    def test_records_are_transformed(self, studio_data):
        import preprocess

        save_tool('def clean(record: dict) -> dict:\n'
                  '    """Strip the whitespace off every text field."""\n'
                  '    return {k: v.strip() if isinstance(v, str) else v\n'
                  '            for k, v in record.items()}\n')
        out = preprocess.apply({"preprocess": "clean"}, [{"company": "  Wayfair  "}])
        assert out == [{"company": "Wayfair"}]

    def test_derived_fields_are_available_to_the_mapping(self, studio_data):
        """Preprocessing runs before records are matched to workflow inputs,
        which is what lets it produce the very field the mapping needs."""
        import preprocess
        import sources

        save_tool('def derive(record: dict) -> dict:\n'
                  '    """Copy `subject` into a `topic` field."""\n'
                  "    return {**record, 'topic': record['subject']}\n")
        records = preprocess.apply({"preprocess": "derive"}, [{"subject": "solar"}])
        mapped = sources.map_to_workflow_inputs(
            records, [{"name": "topic", "required": True}]
        )
        assert mapped == [{"topic": "solar"}]

    def test_a_missing_preprocessor_refuses_the_run(self, studio_data):
        import preprocess

        with pytest.raises(preprocess.PreprocessError) as raised:
            preprocess.apply({"preprocess": "ghost"}, [{"a": 1}])
        assert "ghost" in str(raised.value)

    def test_a_preprocessor_must_take_exactly_one_record(self, studio_data):
        import preprocess

        save_tool('def two_params(record: dict, extra: str) -> dict:\n'
                  '    """Takes more than the one record a preprocessor gets."""\n'
                  '    return record\n')
        with pytest.raises(preprocess.PreprocessError) as raised:
            preprocess.apply({"preprocess": "two_params"}, [{"a": 1}])
        assert "exactly one parameter" in str(raised.value)

    def test_a_non_object_result_refuses_the_run(self, studio_data):
        """Feeding the workflow something it cannot map would produce results
        that look fine and are wrong."""
        import preprocess

        save_tool('def bad(record: dict) -> str:\n'
                  '    """Return something that is not a record."""\n'
                  "    return 'just a string'\n")
        with pytest.raises(preprocess.PreprocessError) as raised:
            preprocess.apply({"preprocess": "bad"}, [{"a": 1}])
        assert "must return an object" in str(raised.value)

    def test_a_failing_preprocessor_names_the_record(self, studio_data):
        import preprocess

        save_tool('def boom(record: dict) -> dict:\n'
                  '    """A preprocessor that always fails."""\n'
                  "    raise ValueError('nope')\n")
        with pytest.raises(preprocess.PreprocessError) as raised:
            preprocess.apply({"preprocess": "boom"}, [{"a": 1}, {"a": 2}])
        assert "record 1" in str(raised.value)


class TestPreprocessIsPartOfTheWorkflow:
    def test_saving_keeps_the_preprocessor(self, studio_data):
        import graphs as graph_store

        created = graph_store.create_graph("With preprocess", "goal")
        body = {**make_graph([make_task("a", outputs=["x"])]),
                "preprocess": "clean_record"}
        saved = graph_store.save_graph(created["id"], body)
        assert saved["preprocess"] == "clean_record"
        # The body renames it, so the workflow now lives under the new id.
        assert graph_store.load_graph(saved["id"])["preprocess"] == "clean_record"

    def test_clearing_it_sticks(self, studio_data):
        import graphs as graph_store

        created = graph_store.create_graph("Clearable", "goal")
        base = make_graph([make_task("a", outputs=["x"])])
        first = graph_store.save_graph(created["id"], {**base, "preprocess": "clean_record"})
        saved = graph_store.save_graph(first["id"], {**base, "preprocess": None})
        assert saved["preprocess"] is None


class TestPrefillFromEarlierRuns:
    """`GET /api/graphs/{id}/inputs` fills a shortened run from run history.

    Exercised through the endpoint because the selection rule — prefer a run
    that covers *everything* — only matters in combination with the run store,
    and that rule is the bug this class exists for.
    """

    @pytest.fixture
    def client(self, studio_data, monkeypatch):
        from fastapi.testclient import TestClient

        import app as studio_app
        import graphs as graph_store

        created = graph_store.create_graph("Prefill", "goal")
        saved = graph_store.save_graph(created["id"], chain())
        self.graph_id = saved["id"]

        def use_runs(runs):
            monkeypatch.setattr(studio_app.runner, "list_runs",
                                lambda graph_id=None: list(runs))
            monkeypatch.setattr(studio_app.runner, "get_run",
                                lambda run_id: next(
                                    (r for r in runs if r["run_id"] == run_id), None))

        self.use_runs = use_runs
        use_runs([])
        return TestClient(studio_app.app)

    def plan(self, client, start_at=None):
        query = f"?start_at={start_at}" if start_at else ""
        response = client.get(f"/api/graphs/{self.graph_id}/inputs{query}")
        assert response.status_code == 200, response.text
        return response.json()

    def test_whole_workflow_asks_for_the_first_input(self, client):
        plan = self.plan(client)
        assert [i["name"] for i in plan["workflow_inputs"]] == ["topic"]
        assert plan["nodes"] == ["a", "b", "c"]

    def test_starting_later_asks_for_the_upstream_output(self, client):
        plan = self.plan(client, "c")
        assert [i["name"] for i in plan["workflow_inputs"]] == ["y"]

    def test_values_come_from_a_previous_run(self, client):
        self.use_runs([
            {"run_id": "r1", "status": "success", "inputs": {"topic": "solar"},
             "nodes": [{"name": "a", "output": {"x": "from a"}},
                       {"name": "b", "output": {"y": "from b"}}]},
        ])
        plan = self.plan(client, "c")
        assert plan["prefill"] == {"y": "from b"}
        assert plan["prefill_from_run"] == "r1"
        assert plan["prefill_missing"] == []

    def test_a_run_covering_everything_beats_a_newer_partial_one(self, client, studio_data):
        """The newest run is usually the one that just failed — it produced some
        of the values and stopped, and taking it left the re-run one input
        short, which is exactly what made the re-run fail again."""
        import graphs as graph_store

        graph_store.save_graph(self.graph_id, join_graph())
        self.use_runs([
            {"run_id": "failed_just_now", "status": "failed", "inputs": {},
             "nodes": [{"name": "a", "output": {"x": "partial x"}}]},
            {"run_id": "good_older", "status": "success", "inputs": {},
             "nodes": [{"name": "a", "output": {"x": "full x"}},
                       {"name": "b", "output": {"y": "full y"}}]},
        ])
        plan = self.plan(client, "join")   # needs both x and y
        assert plan["prefill"] == {"x": "full x", "y": "full y"}
        assert plan["prefill_from_run"] == "good_older"
        assert plan["prefill_missing"] == []

    def test_what_no_run_can_supply_is_reported(self, client):
        self.use_runs([
            {"run_id": "r1", "status": "failed", "inputs": {},
             "nodes": [{"name": "a", "output": {"x": "only x"}}]},
        ])
        plan = self.plan(client, "c")
        assert plan["prefill"] == {}
        assert plan["prefill_missing"] == ["y"]

    def test_an_unknown_start_node_is_refused(self, client):
        response = client.get(f"/api/graphs/{self.graph_id}/inputs?start_at=ghost")
        assert response.status_code == 422
