"""Tests for exporting a workflow as a project and importing one back.

Export writes Python; import reads it. Both were built by repeatedly running
the generated project and fixing what broke, because none of these failures
show up in a static check. The cases below pin the ones that did:

1. Generated code was emitted with the wrong escaping twice, producing a file
   that could not even be imported (an unterminated string literal).
2. A toolkit registered by Studio rather than evoagentx was not resolvable in
   the bundle, and its module computed the repository root from its own depth —
   which changed once it moved into `vendor/`.
3. The dependency walk missed relative imports, so a bundled package's
   `__init__.py` imported siblings that were not there.
4. Import merged `workflow.py` over `graph.json` but re-laid out the whole
   canvas, discarding hand-placed positions.
"""

import ast
import io
import json
import zipfile

import pytest

from conftest import make_graph, make_task


@pytest.fixture
def sample_graph():
    return make_graph(
        [
            make_task("fetch", inputs=["topic"], outputs=["article"]),
            make_task("summarize", inputs=["article"], outputs=["summary"]),
        ],
        edges=[("fetch", "summarize")],
        id="round-trip",
        name="Round trip",
    )


def build(graph):
    import export_api

    _, payload = export_api.build_project(graph)
    return zipfile.ZipFile(io.BytesIO(payload))


def read(archive, name, root="round-trip"):
    return archive.read(f"{root}/{name}").decode("utf-8")


class TestGeneratedProject:
    def test_generated_python_is_syntactically_valid(self, sample_graph):
        archive = build(sample_graph)
        ast.parse(read(archive, "workflow.py"))
        ast.parse(read(archive, "run.py"))

    def test_bundle_carries_the_layers_it_needs(self, sample_graph):
        names = build(sample_graph).namelist()
        for tree in ("evoagentx", "llm", "memory"):
            assert any(f"/vendor/{tree}/" in n for n in names), tree
        assert any(n.endswith("/manifest.json") for n in names)
        assert any(n.endswith("/data/sample_input.json") for n in names)

    def test_manifest_records_provenance(self, sample_graph):
        manifest = json.loads(read(build(sample_graph), "manifest.json"))
        assert manifest["graph_id"] == "round-trip"
        assert "exported_at" in manifest
        # Either a commit or an explicit None; never a silent omission.
        assert "source_commit" in manifest and "source_dirty" in manifest

    def test_tasks_survive_as_python_literals(self, sample_graph):
        import export_api

        code = export_api._graph_from_workflow_py(read(build(sample_graph), "workflow.py"))
        assert [t["name"] for t in code["tasks"]] == ["fetch", "summarize"]
        assert code["edges"] == [{"source": "fetch", "target": "summarize"}]

    def test_requirements_add_memory_extras_only_when_used(self, sample_graph):
        plain = read(build(sample_graph), "requirements.txt")
        assert "EAX_MEMORY_BACKEND" not in plain

        with_memory = make_graph(
            [make_task("a", inputs=["topic"], outputs=["x"], use_long_term_memory=True)],
            id="round-trip",
        )
        text = read(build(with_memory), "requirements.txt")
        assert "EAX_MEMORY_BACKEND=framework" in text
        assert "EAX_MEMORY_BACKEND=langchain" in text


class TestRoundTrip:
    def test_import_restores_the_same_graph(self, sample_graph, studio_data):
        import export_api

        _, payload = export_api.build_project(sample_graph)
        graph, tools, skills, notes = export_api._graph_from_upload("p.zip", payload)

        assert graph["goal"] == sample_graph["goal"]
        assert [t["name"] for t in graph["tasks"]] == ["fetch", "summarize"]
        assert graph["edges"] == sample_graph["edges"]
        assert any("source of truth" in note for note in notes)

    def test_bare_graph_json_is_accepted(self, sample_graph):
        import export_api

        payload = json.dumps(sample_graph).encode("utf-8")
        graph, _, _, notes = export_api._graph_from_upload("graph.json", payload)
        assert [t["name"] for t in graph["tasks"]] == ["fetch", "summarize"]
        assert notes == []

    def test_bare_workflow_py_is_accepted(self, sample_graph):
        import export_api

        source = read(build(sample_graph), "workflow.py")
        graph, _, _, notes = export_api._graph_from_upload("workflow.py",
                                                           source.encode("utf-8"))
        assert [t["name"] for t in graph["tasks"]] == ["fetch", "summarize"]
        assert all("x" in t for t in graph["tasks"])  # laid out from scratch

    def test_a_non_literal_workflow_py_falls_back(self, sample_graph):
        import export_api

        source = read(build(sample_graph), "workflow.py")
        # Someone replaced the literal with something computed.
        broken = source.replace("TASKS = [", "TASKS = list([", 1)
        assert export_api._graph_from_workflow_py(broken) is None

    def test_unreadable_upload_is_refused(self):
        import export_api
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as raised:
            export_api._graph_from_upload("thing.zip", b"not a zip at all")
        assert raised.value.status_code == 422


class TestCodeWinsOnImport:
    def _merge(self, graph, code):
        import export_api

        return export_api._merge_code_into_graph(graph, code)

    def test_code_edits_reach_the_canvas(self):
        graph = make_graph(
            [dict(make_task("a", outputs=["x"]), x=0, y=0)],
            edges=[],
        )
        code = {"goal": "g", "edges": [],
                "tasks": [dict(make_task("a", outputs=["x"]), description="edited")]}
        merged, notes = self._merge(graph, code)
        assert merged["tasks"][0]["description"] == "edited"
        assert any("description" in note for note in notes)

    def test_hand_placed_positions_are_kept(self):
        graph = make_graph([dict(make_task("a", outputs=["x"]), x=640, y=210)])
        code = {"goal": "g", "edges": [], "tasks": [make_task("a", outputs=["x"])]}
        merged, _ = self._merge(graph, code)
        assert (merged["tasks"][0]["x"], merged["tasks"][0]["y"]) == (640, 210)

    def test_new_node_is_placed_next_to_its_upstream(self):
        graph = make_graph([dict(make_task("a", outputs=["x"]), x=100, y=50)])
        code = {
            "goal": "g",
            "edges": [{"source": "a", "target": "b"}],
            "tasks": [make_task("a", outputs=["x"]),
                      make_task("b", inputs=["x"], outputs=["y"])],
        }
        merged, notes = self._merge(graph, code)
        placed = {t["name"]: (t["x"], t["y"]) for t in merged["tasks"]}
        assert placed["a"] == (100, 50)
        assert placed["b"][0] > placed["a"][0]      # one column to the right
        assert placed["b"][1] == placed["a"][1]
        assert any("placed" in note for note in notes)

    def test_source_nodes_survive_because_code_cannot_express_them(self):
        source = {"name": "feed", "kind": "source",
                  "source": {"type": "credit_risk"}, "outputs": [], "x": 0, "y": 0}
        graph = make_graph([source, dict(make_task("a", outputs=["x"]), x=260, y=0)])
        code = {"goal": "g", "edges": [], "tasks": [make_task("a", outputs=["x"])]}
        merged, _ = self._merge(graph, code)
        assert [t["name"] for t in merged["tasks"]] == ["feed", "a"]

    def test_node_deleted_in_code_is_removed(self):
        graph = make_graph([
            dict(make_task("a", outputs=["x"]), x=0, y=0),
            dict(make_task("gone", outputs=["y"]), x=260, y=0),
        ])
        code = {"goal": "g", "edges": [], "tasks": [make_task("a", outputs=["x"])]}
        merged, notes = self._merge(graph, code)
        assert [t["name"] for t in merged["tasks"]] == ["a"]
        assert any("removed" in note for note in notes)

    def test_unchanged_nodes_are_not_reported_as_updated(self):
        """The export drops falsy fields, so a naive dict compare called every
        node changed — the note list was pure noise."""
        graph = make_graph([dict(make_task("a", outputs=["x"]),
                                 x=0, y=0, use_long_term_memory=False,
                                 skill_names=[], tool_names=[])])
        code = {"goal": "g", "edges": [], "tasks": [make_task("a", outputs=["x"])]}
        _, notes = self._merge(graph, code)
        assert not any("updated" in note for note in notes)
