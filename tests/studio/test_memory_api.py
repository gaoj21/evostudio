"""The Memory tab shows what the runs wrote, whichever kind of store.

It listed vector corpora only; a workflow whose nodes keep tables showed
"(no memory stores yet)" while 41 rows sat on disk.
"""

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from backend.api import app as studio_app
    from backend.api import graphs as graph_store, table_store
    monkeypatch.setattr(table_store, "TABLES_DIR", tmp_path / "tables")
    monkeypatch.setattr(graph_store, "graph_exists", lambda gid: gid == "g1")
    table_store.upsert("g1", "decide", "Sleep Number", "2026-03-13",
                       {"inputs": {"company": "Sleep Number"}, "outputs": {"decision": "alert"}}, "t1")
    table_store.upsert("g1", "decide", "Sleep Number", "2026-02-11",
                       {"inputs": {"company": "Sleep Number"}, "outputs": {"decision": "suppress"}}, "t0")
    table_store.upsert("g1", "decide", "Lucid", "2026-01-16",
                       {"inputs": {"company": "Lucid"}, "outputs": {"decision": "suppress"}}, "t2")
    table_store.upsert("g1", "investigate", "Lucid", "2026-01-16", {"outputs": {"context": "…"}}, "t2")
    return TestClient(studio_app.app)


def test_table_stores_are_listed_with_their_size_and_subjects(client):
    res = client.get("/api/graphs/g1/memory/agents").json()
    stores = {s["node"]: s for s in res["stores"]}
    assert stores["decide"]["kind"] == "table"
    assert stores["decide"]["count"] == 3
    assert stores["decide"]["subjects"] == ["Lucid", "Sleep Number"]
    assert stores["investigate"]["count"] == 1
    assert res["agents"] == []                      # no vector corpora here


def test_a_table_reads_as_a_timeline_per_subject(client):
    res = client.get("/api/graphs/g1/memory", params={"agent": "decide"}).json()
    assert res["kind"] == "table"
    assert [(e["subject"], e["at"]) for e in res["entries"]] == [
        ("Lucid", "2026-01-16"), ("Sleep Number", "2026-02-11"), ("Sleep Number", "2026-03-13")]
    assert res["entries"][2]["content"]["outputs"]["decision"] == "alert"


def test_the_query_narrows_to_a_subject(client):
    res = client.get("/api/graphs/g1/memory", params={"agent": "decide", "q": "sleep"}).json()
    assert {e["subject"] for e in res["entries"]} == {"Sleep Number"}


def test_an_unknown_workflow_is_404(client):
    assert client.get("/api/graphs/nope/memory/agents").status_code == 404
