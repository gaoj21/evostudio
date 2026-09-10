"""Phase 1 of RUN_INPUT_FLOW_PLAN.md: what you confirm is what runs.

A run used to be shaped by three separate rules — the canvas, the backend's
input derivation, and the framework's own inference — and the dialog could
show one thing while the request ran another. These pin the contract: a run
is planned first, the plan is immutable and identified, and everything that
can be checked is checked *before* a run id exists. Written failing, against
the behaviour these replace.
"""

import pytest

from conftest import make_graph, make_task


def chain():
    return make_graph(
        [
            make_task("a", inputs=["topic"], outputs=["x"]),
            make_task("b", inputs=["x"], outputs=["y"]),
            make_task("c", inputs=["y"], outputs=["z"]),
        ],
        edges=[("a", "b"), ("b", "c")],
    )


def typed_graph():
    """One node whose inputs carry real types, so a wrong one can be caught."""
    task = make_task("score", inputs=["text"], outputs=["verdict"])
    task["inputs"] = [
        {"name": "text", "type": "str", "description": "t", "required": True},
        {"name": "limit", "type": "int", "description": "n", "required": True},
    ]
    task["prompt"] = "Score {text} up to {limit}"
    return make_graph([task])


def fed_graph(n=4):
    """A canvas source feeding one LLM node, a parked draft, and a source
    that is wired to nothing at all."""
    feed = make_task("feed", outputs=["company", "news_batch"])
    feed["kind"] = "source"
    feed["source"] = {"type": "credit_risk", "split": "test", "n": n, "seed": 1}
    lonely = make_task("lonely", outputs=["ticker"])
    lonely["kind"] = "source"
    lonely["source"] = {"type": "credit_risk", "split": "test", "n": 1, "seed": 1}
    judge = make_task("judge", inputs=["company", "news_batch"], outputs=["verdict"])
    draft = make_task("draft", inputs=["company"], outputs=["note"])
    return make_graph([feed, lonely, judge, draft], edges=[("feed", "judge")])


@pytest.fixture
def client(studio_data, monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app as studio_app

    launched = []

    def fake_start(graph, inputs, **kw):
        launched.append({"graph": graph, "inputs": inputs, **kw})
        return "run-fake"

    monkeypatch.setattr(studio_app.runner, "start_run", fake_start)
    monkeypatch.setattr(studio_app.runner, "list_runs", lambda graph_id=None: [])
    monkeypatch.setattr(studio_app.runner, "get_run", lambda run_id: None)
    # A run may probe the source before starting; a *plan* must not — it is
    # described by its own configuration and must be cheap enough to re-ask
    # on every edit. Probes are counted so a plan can be held to that.
    probed = []
    monkeypatch.setattr(studio_app.sources, "records_from_source_node",
                        lambda node: probed.append(node.get("name")) or [{}])
    tc = TestClient(studio_app.app)
    tc.launched = launched
    tc.probed = probed
    return tc


def save(client, graph):
    from backend.api import graphs as graph_store

    created = graph_store.create_graph(graph.get("name", "G"), graph.get("goal", "g"))
    return graph_store.save_graph(created["id"], graph)["id"]


def plan(client, graph_id, **body):
    res = client.post(f"/api/graphs/{graph_id}/run-plan", json=body)
    assert res.status_code == 200, res.text
    return res.json()


class TestNothingStartsBeforeItIsChecked:
    """Every refusal happens before a run id exists: a run that is created and
    then dies on a missing value is the thing this replaces."""

    def test_an_unknown_start_point_is_refused_not_crashed(self, client):
        gid = save(client, chain())
        res = client.post(f"/api/graphs/{gid}/run",
                          json={"inputs": {"y": "v"}, "start_at": ["nope"]})

        assert res.status_code == 422, res.text
        assert client.launched == []

    def test_a_missing_required_input_is_refused_before_any_run_exists(self, client):
        gid = save(client, chain())
        res = client.post(f"/api/graphs/{gid}/run", json={"inputs": {}})

        assert res.status_code == 422, res.text
        assert "topic" in res.text
        assert client.launched == []

    def test_a_wrongly_typed_input_is_refused(self, client):
        gid = save(client, typed_graph())
        res = client.post(f"/api/graphs/{gid}/run",
                          json={"inputs": {"text": "hello", "limit": "lots"}})

        assert res.status_code == 422, res.text
        assert "limit" in res.text
        assert client.launched == []

    def test_a_correct_request_still_starts(self, client):
        gid = save(client, chain())
        res = client.post(f"/api/graphs/{gid}/run", json={"inputs": {"topic": "rates"}})

        assert res.status_code == 200, res.text
        assert res.json()["run_id"] == "run-fake"
        assert client.launched[0]["inputs"] == {"topic": "rates"}

    def test_the_preprocessor_runs_even_when_there_are_no_inputs(self, client, monkeypatch):
        # A source-fed workflow has no external inputs, and skipping the
        # preprocessor for an empty object silently ran it unprocessed.
        from backend.api import app as studio_app

        seen = []

        def spy(graph, records):
            seen.append(records)
            return records

        monkeypatch.setattr(studio_app.preprocess, "apply", spy)
        gid = save(client, fed_graph())
        res = client.post(f"/api/graphs/{gid}/run", json={"inputs": {}})

        assert res.status_code == 200, res.text
        assert seen == [[{}]]


class TestThePlanIsTheOnlyTruth:
    def test_a_plan_names_the_revision_it_was_made_from(self, client):
        gid = save(client, chain())
        first = plan(client, gid)

        assert first["plan_id"]
        assert first["graph_revision"]
        assert plan(client, gid)["plan_id"] == first["plan_id"]   # stable

    def test_editing_the_graph_changes_the_plan(self, client):
        gid = save(client, chain())
        before = plan(client, gid)["plan_id"]

        from backend.api import graphs as graph_store
        edited = chain()
        edited["tasks"][0]["inputs"].append(
            {"name": "region", "type": "str", "description": "r", "required": True})
        edited["tasks"][0]["prompt"] += " in {region}"
        graph_store.save_graph(gid, edited)

        assert plan(client, gid)["plan_id"] != before

    def test_a_run_with_a_stale_plan_is_refused_as_such(self, client):
        gid = save(client, chain())
        stale = plan(client, gid)["plan_id"]

        from backend.api import graphs as graph_store
        edited = chain()
        edited["goal"] = "something else now"
        graph_store.save_graph(gid, edited)

        res = client.post(f"/api/graphs/{gid}/run",
                          json={"plan_id": stale, "inputs": {"topic": "t"}})

        assert res.status_code == 409, res.text
        assert res.json()["detail"]["code"] == "plan_stale"
        assert client.launched == []

    def test_a_run_with_the_current_plan_is_accepted(self, client):
        gid = save(client, chain())
        current = plan(client, gid)["plan_id"]
        res = client.post(f"/api/graphs/{gid}/run",
                          json={"plan_id": current, "inputs": {"topic": "t"}})

        assert res.status_code == 200, res.text

    def test_a_run_without_a_plan_still_works_but_says_so(self, client):
        # One migration cycle: the old request shape is planned on the spot
        # and told that it is on the way out.
        gid = save(client, chain())
        res = client.post(f"/api/graphs/{gid}/run", json={"inputs": {"topic": "t"}})

        assert res.status_code == 200
        assert "deprecat" in " ".join(res.json().get("warnings", [])).lower()

    def test_the_plan_lists_what_the_run_will_ask_for(self, client):
        gid = save(client, chain())
        p = plan(client, gid)
        assert [i["name"] for i in p["inputs"]] == ["topic"]

        later = plan(client, gid, start_at=["b"])
        assert [i["name"] for i in later["inputs"]] == ["x"]
        assert later["plan_id"] != p["plan_id"]


class TestStartPointsThatCanActuallyRun:
    def test_a_source_that_reaches_nothing_is_not_offered(self, client):
        # A source only yields data. Starting at one that feeds no LLM or tool
        # node runs nothing; offering it was a guaranteed failure.
        gid = save(client, fed_graph())
        p = plan(client, gid)

        assert "judge" in p["start_points"]
        assert "feed" in p["start_points"]         # it reaches judge
        assert "lonely" not in p["start_points"]   # reaches nothing

    def test_a_source_wired_to_nothing_is_not_a_node_of_the_run(self, client):
        # The runner skips it; found live, where the plan for the real
        # workflow listed two such sources as if they would execute.
        gid = save(client, fed_graph())
        assert "lonely" not in [n["name"] for n in plan(client, gid)["nodes"]]

    def test_a_disabled_draft_is_neither_a_node_nor_a_start_point(self, client):
        # Migration turned the old edgeless draft into `enabled: false`. Off
        # means off: not part of the run and not offered as a place to start.
        # Enable it and it becomes a one-node run, which is how an alternative
        # node gets tried.
        from backend.api import graphs as graph_store
        gid = save(client, fed_graph())
        p = plan(client, gid)
        assert "draft" not in [n["name"] for n in p["nodes"]]
        assert "draft" not in p["start_points"]

        g = graph_store.load_graph(gid)
        next(t for t in g["tasks"] if t["name"] == "draft")["enabled"] = True
        graph_store.save_graph(gid, g)
        alone = plan(client, gid, start_at=["draft"])
        assert [n["name"] for n in alone["nodes"]] == ["draft"]

    def test_starting_at_a_start_point_that_reaches_nothing_is_refused(self, client):
        gid = save(client, fed_graph())
        res = client.post(f"/api/graphs/{gid}/run-plan", json={"start_at": ["lonely"]})
        assert res.status_code == 422, res.text
        assert "Nothing would run" in res.text


class TestASourceSaysHowManyRecordsItYields:
    def test_cardinality_comes_from_the_source_config(self, client):
        gid = save(client, fed_graph(n=4))
        p = plan(client, gid)

        assert client.probed == [], "a plan must not load a dataset"
        assert p["source"]["node"] == "feed"
        assert p["source"]["cardinality"] == 4
        assert p["source"]["single_policy"] == "first"

    def test_a_workflow_without_a_source_has_none(self, client):
        gid = save(client, chain())
        assert plan(client, gid)["source"] is None

    def test_more_than_one_record_is_flagged_for_a_single_run(self, client):
        # Four records and "single run" used to mean the first one, quietly.
        gid = save(client, fed_graph(n=4))
        p = plan(client, gid, mode="single")
        assert any("4" in w and "batch" in w.lower() for w in p["warnings"]), p["warnings"]


class TestThePlanCarriesWhatHistoryCanSupply:
    def test_prefill_rides_along_so_the_dialog_reads_one_thing(self, client, monkeypatch):
        from backend.api import app as studio_app

        gid = save(client, chain())
        runs = [{"run_id": "r1", "graph_id": gid, "status": "success",
                 "inputs": {"topic": "rates"},
                 "nodes": [{"name": "a", "output": {"x": "from a"}}]}]
        monkeypatch.setattr(studio_app.runner, "list_runs", lambda graph_id=None: runs)
        monkeypatch.setattr(studio_app.runner, "get_run", lambda run_id: runs[0])

        later = plan(client, gid, start_at=["b"])
        assert later["prefill"] == {"x": "from a"}
        assert later["prefill_from_run"] == "r1"
        assert later["prefill_missing"] == []


class TestASingleRunPicksItsRecordOutLoud:
    """Phase 2: four records and "single run" used to mean the first one,
    quietly. The choice is now explicit and checked."""

    def test_a_chosen_record_reaches_the_runner(self, client):
        gid = save(client, fed_graph(n=4))
        res = client.post(f"/api/graphs/{gid}/run", json={"inputs": {}, "record": 2})

        assert res.status_code == 200, res.text
        assert client.launched[0]["record_index"] == 2

    def test_a_record_beyond_the_source_is_refused(self, client):
        gid = save(client, fed_graph(n=4))
        res = client.post(f"/api/graphs/{gid}/run", json={"inputs": {}, "record": 7})

        assert res.status_code == 422, res.text
        assert client.launched == []

    def test_a_record_makes_no_sense_without_a_source(self, client):
        gid = save(client, chain())
        res = client.post(f"/api/graphs/{gid}/run",
                          json={"inputs": {"topic": "t"}, "record": 0})
        assert res.status_code == 422, res.text

    def test_the_plan_can_list_the_records_when_asked(self, client):
        # Only when asked: this probes the dataset, which a plan must not do
        # on every edit. Asked for, it is the "choose a record" list.
        gid = save(client, fed_graph(n=4))
        p = plan(client, gid, include_records=True)

        assert client.probed == ["feed"]
        assert [r["index"] for r in p["records"]] == [0]      # the stub yields one
        assert isinstance(p["records"][0]["summary"], dict)

    def test_not_asked_the_plan_lists_none_and_probes_nothing(self, client):
        gid = save(client, fed_graph(n=4))
        p = plan(client, gid)
        assert "records" not in p or p["records"] is None
        assert client.probed == []


class TestStartFromSaysWhatIsSkipped:
    def test_nodes_before_the_start_are_listed_as_skipped(self, client):
        gid = save(client, chain())
        later = plan(client, gid, start_at=["b"])
        assert later["skipped"] == ["a"]

    def test_a_whole_run_skips_only_what_is_not_in_the_pipeline(self, client):
        gid = save(client, fed_graph())
        assert sorted(plan(client, gid)["skipped"]) == ["draft", "lonely"]


def test_starting_after_source_does_not_probe_skipped_data(client):
    gid = save(client, fed_graph())
    planned = plan(client, gid, start_at=["judge"])
    response = client.post(f"/api/graphs/{gid}/run", json={
        "inputs": {"company": "Example", "news_batch": "Provided material"},
        "start_at": ["judge"], "plan_id": planned["plan_id"],
    })
    assert response.status_code == 200, response.text
    assert client.probed == []
    assert len(client.launched) == 1


def test_incompatible_memory_sources_are_visible_in_run_plan(client):
    graph = chain()
    graph['tasks'][0].update(use_long_term_memory=True, memory={'match': 'topic'})
    graph['tasks'][1].update(use_long_term_memory=True, memory={'read_from': ['a']})
    gid = save(client, graph)
    result = plan(client, gid)
    assert any('Align storage types' in warning for warning in result['warnings'])
