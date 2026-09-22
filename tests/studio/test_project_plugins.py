"""Project plugins: task-specific code reaches Studio without Studio knowing it.

A folder under EAX_STUDIO_PROJECTS with a studio_plugin.py adds palette
presets, templates, toolkits and Input types. These tests write such a
project into a temporary directory and check each contribution appears
through the ordinary API — and that a broken project is reported, not fatal.
"""

import pytest

from conftest import make_graph, make_task

# Tool classes register globally by name, so they live in the project's own
# module (imported once, as a real project's would be) rather than in the
# plugin file, which reload() executes again.
TOOLS = '''
from evoagentx.tools.tool import Tool, Toolkit


class EchoTool(Tool):
    name: str = "ledger_echo"
    description: str = "Echo a value back."
    inputs: dict = {"value": {"type": "string", "description": "anything"}}
    required: list = ["value"]

    def __call__(self, value: str):
        return {"echo": value}


class LedgerToolkit(Toolkit):
    def __init__(self, name: str = "LedgerToolkit"):
        super().__init__(name=name, tools=[EchoTool()])
'''

PLUGIN = '''
NAME = "Ledger demo"
DESCRIPTION = "A test project."


def presets():
    return [{"type": "ledger_judge", "label": "Judge an entry",
             "defaults": {"name": "judge", "prompt": "Judge {entry}",
                          "inputs": [{"name": "entry", "type": "str"}],
                          "outputs": [{"name": "verdict", "type": "str"}]}}]


def templates():
    return [{"id": "ledger-template", "name": "Ledger review",
             "description": "Review each ledger entry.",
             "graph": {"name": "Ledger review", "goal": "review", "tasks": [], "edges": []}}]


def toolkits():
    def factory(**kwargs):
        from ledger_demo.tools import LedgerToolkit
        return LedgerToolkit(**kwargs)
    return {"LedgerToolkit": {"factory": factory, "description": "Ledger helpers.",
                              "requires": [], "tool_names": ["ledger_echo"]}}


ROWS = [{"account": a, "day": d, "entry": f"{a}:{d}"}
        for a in ("a", "b") for d in ("2026-01-01", "2026-01-02", "2026-01-03")]


def _records(config):
    n = int(config.get("n") or 0)
    return ROWS[:n] if n else list(ROWS)


def _info(params):
    return {"accounts": sorted({r["account"] for r in ROWS}), "asked": params}


def source_types():
    return {"ledger": {
        "label": "Ledger",
        "description": "Ledger rows, one trajectory per account.",
        "config": [{"name": "n", "label": "Rows", "type": "number", "default": 0}],
        "outputs": ["account", "day", "entry"],
        "records": _records,
        "info": _info,
        "sequence": {"group": "account", "order": "day"},
        "watch_key": "account",
    }}
'''


@pytest.fixture
def projects(tmp_path, monkeypatch):
    """A projects folder holding one working and one broken project."""
    from backend.features import plugins
    root = tmp_path / "projects"
    (root / "ledger_demo").mkdir(parents=True)
    (root / "ledger_demo" / "studio_plugin.py").write_text(PLUGIN)
    (root / "ledger_demo" / "tools.py").write_text(TOOLS)
    (root / "broken_demo").mkdir()
    (root / "broken_demo" / "studio_plugin.py").write_text(
        "import a_module_that_does_not_exist\n")
    monkeypatch.setenv("EAX_STUDIO_PROJECTS", str(root))
    plugins.reload()
    yield root
    monkeypatch.delenv("EAX_STUDIO_PROJECTS")
    plugins.reload()


@pytest.fixture
def client(projects, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from backend.api import app, graphs
    monkeypatch.setattr(graphs, "GRAPHS_DIR", tmp_path / "graphs")
    return TestClient(app.app)


def ledger_graph(graph_id="g-ledger"):
    from backend.api import graphs
    feed = make_task("feed", outputs=["entry"])
    feed.update(kind="source", source={"type": "ledger", "n": 0})
    graph = make_graph([feed, make_task("judge", inputs=["entry"], outputs=["verdict"])],
                       edges=[("feed", "judge")], id=graph_id)
    graphs.save_graph(graph_id, graph)
    return graph


class TestWhatAProjectAdds:
    def test_its_presets_are_in_the_palette_under_its_name(self, client):
        presets = {p.get("type"): p for p in client.get("/api/palette").json()["templates"]}
        assert presets["ledger_judge"]["group"] == "Ledger demo"
        assert presets["ledger_judge"]["defaults"]["prompt"] == "Judge {entry}"

    def test_its_templates_are_listed_and_served(self, client):
        listed = client.get("/api/templates").json()["templates"]
        assert {"id": "ledger-template", "name": "Ledger review",
                "description": "Review each ledger entry."} in listed
        assert client.get("/api/templates/ledger-template").json()["graph"]["goal"] == "review"

    def test_its_toolkits_are_tools(self, client):
        from backend.api import tools_registry
        tools = {t["name"]: t for t in client.get("/api/tools").json()["tools"]}
        assert tools["LedgerToolkit"]["available"] is True
        assert [s["name"] for s in tools["LedgerToolkit"]["tools"]] == ["ledger_echo"]
        assert tools_registry.call_tool("ledger_echo", {"value": "x"}) == {"echo": "x"}

    def test_its_input_type_is_a_local_source_with_its_sequence(self, client):
        types = {t["type"]: t for t in client.get("/api/sources").json()["source_types"]}
        ledger = types["ledger"]
        assert ledger["local"] is True and ledger["has_info"] is True
        assert ledger["sequence"] == {"group": "account", "order": "day"}
        assert ledger["project"] == "ledger_demo"
        # Hooks are code, not schema: they never reach the canvas.
        assert not {"records", "info", "watch_key"} & ledger.keys()

    def test_its_info_hook_answers_with_the_query(self, client):
        res = client.get("/api/sources/ledger/info", params={"split": "dev"})
        assert res.status_code == 200, res.text
        assert res.json() == {"accounts": ["a", "b"], "asked": {"split": "dev"}}

    def test_a_type_without_info_is_a_404(self, client):
        assert client.get("/api/sources/gdelt_news/info").status_code == 404
        assert client.get("/api/sources/no_such_type/info").status_code == 404

    def test_its_records_run(self, client):
        probe = client.post("/api/sources/probe", json={"source": {"type": "ledger", "n": 2}})
        assert probe.status_code == 200, probe.text
        assert probe.json()["records"] == 2
        assert probe.json()["fields"] == ["account", "day", "entry"]

    def test_a_batch_over_it_counts_trajectories(self, client):
        ledger_graph()
        body = client.post("/api/graphs/g-ledger/run-batch/preview",
                           json={"source": "canvas"}).json()
        assert body["total"] == 6
        assert body["sequence"] == {"group": "account", "order": "day"}
        assert (body["samples"], body["steps"]) == (2, 3)
        assert body["dates"] == ["2026-01-01", "2026-01-03"]


class TestABrokenProject:
    def test_it_is_reported_in_the_feature_catalog(self, client):
        projects = {p["name"]: p for p in client.get("/api/features").json()["projects"]}
        assert projects["ledger_demo"]["available"] is True
        assert projects["ledger_demo"]["label"] == "Ledger demo"
        assert projects["broken_demo"]["available"] is False
        assert "a_module_that_does_not_exist" in projects["broken_demo"]["unavailable_reason"]

    def test_it_does_not_take_the_rest_down(self, client):
        assert client.get("/api/palette").status_code == 200
        types = {t["type"] for t in client.get("/api/sources").json()["source_types"]}
        assert "ledger" in types and "gdelt_news" in types

    def test_a_hook_that_raises_is_reported_and_contributes_nothing(
            self, projects, client):
        from backend.features import plugins
        (projects / "ledger_demo" / "studio_plugin.py").write_text(
            PLUGIN + "\n\ndef templates():\n    raise RuntimeError('no templates today')\n")
        plugins.reload()
        listed = client.get("/api/templates").json()["templates"]
        assert "ledger-template" not in [t["id"] for t in listed]
        projects_ = {p["name"]: p for p in client.get("/api/features").json()["projects"]}
        assert "no templates today" in projects_["ledger_demo"]["unavailable_reason"]


def test_the_repository_projects_register_credit_risk():
    """The real credit-risk project is a plugin like any other."""
    from backend.features import plugins
    plugins.reload()
    risk = plugins.source_type("credit_risk")
    assert risk is not None and risk["project"] == "credit_risk"
    assert risk["sequence"] == {"group": "sample_id", "order": "as_of"}
    assert "ObligorMatchToolkit" in plugins.toolkits()
    assert any(p["type"].startswith("cr_") for p in plugins.presets())
