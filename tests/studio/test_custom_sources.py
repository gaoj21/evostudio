"""An input source can be a custom tool.

The source palette used to be closed: the credit-risk feed and three API
fetchers. A toolkit's author can now mark a function as an input source —
anything returning a record or records — and it appears beside them, with
its parameters as the node's config and its outputs found by running it.
"""

import pytest

from conftest import make_graph, make_task
from backend.api import custom_tools

FEED = '''"""A tiny feed of tickers."""

def tickers(limit: int = 2) -> list:
    """The next few tickers to look at.

    Args:
        limit: how many
    """
    return [{"symbol": s, "note": "n"} for s in ["AAA", "BBB", "CCC"][:limit]]

def shout(text: str) -> str:
    """Upper-case some text.

    Args:
        text: the text
    """
    return text.upper()
'''


def save(code, name="feedkit", sources=("tickers",)):
    return custom_tools.save_custom_tool(
        custom_tools.validate_spec({"code": code, "name": name, "sources": list(sources)}, []))


class TestMarkingAToolAsASource:
    def test_a_marked_function_becomes_a_source_type(self, studio_data):
        save(FEED)
        from backend.api.source_apis import all_source_types
        types = all_source_types()
        assert "custom:tickers" in types
        assert types["custom:tickers"]["config"][0] == {
            "name": "limit", "label": "limit", "type": "number", "default": 0, "required": False}
        assert "custom:shout" not in types           # not marked, not offered

    def test_only_exported_functions_can_be_marked(self, studio_data):
        with pytest.raises(custom_tools.CustomToolError):
            save(FEED, sources=("nothing",))

    def test_the_mark_is_kept_and_listed(self, studio_data):
        save(FEED)
        assert custom_tools.list_custom_tools()[0]["sources"] == ["tickers"]


class TestRunningACustomSource:
    def test_a_list_of_dicts_is_records(self, studio_data):
        save(FEED)
        from backend.api import sources
        got = sources.records_from_source_node(
            {"name": "feed", "kind": "source", "source": {"type": "custom:tickers", "limit": 2}})
        assert got == [{"symbol": "AAA", "note": "n"}, {"symbol": "BBB", "note": "n"}]

    def test_a_single_dict_is_one_record(self, studio_data):
        save('''def one() -> dict:
    """One record."""
    return {"a": 1}
''', name="onekit", sources=("one",))
        from backend.api import sources
        assert sources.records_from_source_node(
            {"name": "f", "kind": "source", "source": {"type": "custom:one"}}) == [{"a": 1}]

    def test_anything_else_is_refused_with_the_reason(self, studio_data):
        save('''def bad() -> str:
    """Not a record."""
    return "just text"
''', name="badkit", sources=("bad",))
        from backend.api import sources
        with pytest.raises(sources.SourceError) as err:
            sources.records_from_source_node({"name": "f", "kind": "source", "source": {"type": "custom:bad"}})
        assert "must return a record" in str(err.value)

    def test_a_workflow_using_it_validates(self, studio_data):
        save(FEED)
        from backend.api import graphs as graph_store
        feed = make_task("feed", outputs=["symbol", "note"]); feed["kind"] = "source"
        feed["source"] = {"type": "custom:tickers", "limit": 1}
        g = make_graph([feed, make_task("judge", inputs=["symbol"], outputs=["v"])],
                       edges=[("feed", "judge")])
        graph_store.validate_graph(g)                # no error


class TestTheCanvasSeesIt:
    @pytest.fixture
    def client(self, studio_data):
        from fastapi.testclient import TestClient
        from backend.api import app as studio_app
        save(FEED)
        return TestClient(studio_app.app)

    def test_it_is_listed_with_the_built_in_sources(self, client):
        types = {s["type"] for s in client.get("/api/sources").json()["source_types"]}
        assert {"credit_risk", "custom:tickers"} <= types

    def test_it_is_in_the_palette(self, client):
        presets = client.get("/api/palette").json()["sources"]
        mine = next(p for p in presets if p["type"] == "source_custom:tickers")
        assert mine["defaults"]["source"]["type"] == "custom:tickers"

    def test_probing_finds_the_outputs(self, client):
        res = client.post("/api/sources/probe", json={"source": {"type": "custom:tickers", "limit": 1}})
        assert res.status_code == 200, res.text
        assert res.json()["fields"] == ["note", "symbol"]
        assert res.json()["records"] == 1

    def test_probing_a_broken_source_says_why(self, client):
        res = client.post("/api/sources/probe", json={"source": {"type": "custom:nope"}})
        assert res.status_code == 422
