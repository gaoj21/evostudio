"""An evaluator is the user's own code, kept with the workflow: what it can
rely on. Evaluation is not on the canvas — the list lives on the graph
document and the Evaluate & Evolve panel owns it."""
import time

import pytest

from backend.api.sources import SourceError
from backend.features.evaluation import evaluator_tools as tools

RUN = {"run_id": "r1", "status": "success", "inputs": {"text": "hi"},
       "nodes": [{"name": "work", "output": {"answer": "HI"}}], "result": {"answer": "HI"}}


def graph(entry):
    return {"tasks": [{"name": "work", "inputs": [], "outputs": [{"name": "answer"}]}],
            "edges": [], "evaluators": [{"name": "check", **entry}]}


def evaluate(code, runs=(RUN,), **entry):
    doc = graph({"code": code, "timing": "batch", **entry})
    return tools.evaluate_runs(doc, list(runs), timing={"batch"})["check"]


RATE = '''def evaluate(records):
    ok = sum(r["status"] == "success" for r in records)
    return {"metrics": {"completion_rate": ok / len(records), "count": len(records)}}'''


class TestTheObjective:
    def test_without_a_chosen_metric_the_first_returned_is_used_and_said(self):
        r = evaluate(RATE)
        assert r["status"] == "success"
        assert r["objective"] == {"metric": "completion_rate", "direction": "maximize", "chosen": "first metric returned"}

    def test_a_chosen_metric_the_code_does_not_return_names_what_it_did_return(self):
        r = evaluate(RATE, metric="accuracy")
        assert r["status"] == "failed"
        assert "'accuracy'" in r["error"] and "completion_rate" in r["error"] and "count" in r["error"]

    def test_evolve_needs_the_objective_chosen(self):
        from backend.features.evaluation import canvas_evolution
        g = graph({"code": RATE, "timing": "batch"})
        with pytest.raises(SourceError, match="Choose the objective metric"):
            canvas_evolution.select(g, "check")
        g["evaluators"][0]["metric"] = "completion_rate"
        assert canvas_evolution.select(g, "check")["name"] == "check"

    def test_evolve_accepts_the_canvas_prefixed_name_and_the_bare_one(self):
        from backend.features.evaluation import canvas_evolution
        g = graph({"code": RATE, "timing": "batch", "metric": "completion_rate"})
        assert canvas_evolution.select(g, "check")["name"] == "check"
        assert canvas_evolution.select(g, "canvas:check")["name"] == "check"


class TestTiming:
    def test_one_default_everywhere(self):
        assert tools.timing_of({}) == "run" == tools.DEFAULT_TIMING
        assert tools.evaluate_runs(graph({"code": RATE}), [RUN], timing={"run"})
        assert tools.evaluate_runs(graph({"code": RATE}), [RUN], timing={"batch"}) == {}

    def test_manual_runs_only_when_asked(self):
        g = graph({"code": RATE, "timing": "manual"})
        assert tools.evaluate_runs(g, [RUN], timing={"run"}) == {}
        assert tools.evaluate_runs(g, [RUN], timing={"batch"}) == {}
        assert tools.evaluate_runs(g, [RUN], ["check"])["check"]["status"] == "success"

    def test_the_node_timing_is_gone(self):
        with pytest.raises(SourceError, match="timing must be one of"):
            tools.validate_graph(graph({"code": RATE, "timing": "node"}))

    @pytest.mark.parametrize("value,ok", [(120, True), (10, True), (3600, True), (5, False), (True, False)])
    def test_time_limit_bounds(self, value, ok):
        g = graph({"code": RATE, "timeout": value})
        if ok:
            tools.validate_graph(g)
        else:
            with pytest.raises(SourceError, match="time limit"):
                tools.validate_graph(g)

    def test_running_over_the_limit_says_which_limit(self, monkeypatch):
        from backend.features.chat import chat_control
        def slow(*a, **k):
            assert k["timeout"] == 45
            raise TimeoutError("evaluate_python worker exceeded 45 seconds")
        monkeypatch.setattr(chat_control, "worker", slow)
        r = evaluate(RATE, timeout=45)
        assert r["status"] == "failed" and "45-second limit" in r["error"]


class TestWritingTheCode:
    def test_an_error_names_the_line_in_the_users_code(self):
        r = evaluate('''def evaluate(records):
    total = 0
    for record in records:
        total += record["missing"]
    return {"metrics": {"score": total}}''')
        assert r["status"] == "failed"
        assert "KeyError: 'missing'" in r["error"]
        assert 'line 4, in evaluate: total += record["missing"]' in r["error"]

    def test_printed_output_is_kept(self):
        r = evaluate('''def evaluate(records):
    print("looking at", len(records), "records")
    return {"metrics": {"score": 1.0}}''')
        assert r["logs"] == "looking at 1 records\n"

    def test_output_printed_before_an_error_comes_with_it(self):
        r = evaluate('''def evaluate(records):
    print("first record:", records[0]["run_id"])
    raise ValueError("cannot score this")''')
        assert "ValueError: cannot score this" in r["error"]
        assert "Output before the error:\nfirst record: r1" in r["error"]

    def test_a_class_factory_error_points_into_the_method(self):
        r = evaluate('''class Scorer:
    def evaluate(self, records):
        return {"metrics": {"score": 1 / 0}}

def build_evaluator():
    return Scorer()''')
        assert "ZeroDivisionError" in r["error"] and "line 3, in evaluate" in r["error"]


class TestFailedRunsAreEvaluatedToo:
    def test_a_failed_run_can_be_evaluated_and_never_evaluates_itself(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        from backend.api import app as studio_app, graphs as graph_store
        from backend.features.workflow import runner
        from evoagentx.models.model_configs import LiteLLMConfig
        monkeypatch.setattr(graph_store, "GRAPHS_DIR", tmp_path / "graphs")
        monkeypatch.setattr(runner, "RUNS_DIR", tmp_path / "runs")

        async def failing(agent, task, inputs, state):
            raise ValueError("model failed")

        class Stub:
            config = LiteLLMConfig(model="deepseek/deepseek-chat", deepseek_key="test-only")
        monkeypatch.setattr(runner, "execute_llm_node", failing)
        monkeypatch.setattr(runner, "_make_llm", lambda **kw: Stub())
        c = TestClient(studio_app.app)
        gid = c.post("/api/graphs", json={"name": "fails", "goal": "g"}).json()["id"]
        g = {"id": gid, "name": "fails", "goal": "g", "flow_version": 3, "edges": [], "tasks": [
            {"name": "work", "description": "d", "prompt": "{text}", "parse_mode": "json",
             "inputs": [{"name": "text", "type": "str", "description": "t", "required": True}],
             "outputs": [{"name": "answer", "type": "str", "description": "a", "required": True}]}],
            "evaluators": [{"name": "check", "code": RATE, "timing": "run", "metric": "completion_rate"}]}
        assert c.put(f"/api/graphs/{gid}", json=g).status_code == 200
        plan = c.post(f"/api/graphs/{gid}/run-plan", json={"start_at": [], "mode": "single"}).json()
        rid = c.post(f"/api/graphs/{gid}/run", json={"inputs": {"text": "x"}, "plan_id": plan["plan_id"]}).json()["run_id"]
        for _ in range(200):
            state = runner.get_run(rid)
            if state["status"] not in ("running", "pending"):
                break
            time.sleep(0.05)
        assert state["status"] == "failed"
        assert not state.get("evaluations")        # never on its own, whatever the saved timing
        # A failure is a result the evaluation code has to be able to count.
        report = c.post(f"/api/graphs/{gid}/evaluators/run", json={"run_id": rid})
        assert report.status_code == 200, report.text
        assert report.json()["evaluations"]["check"]["metrics"]["completion_rate"] == 0.0


class TestEvaluatingASavedBatch:
    def test_the_report_is_kept_with_the_batch_and_listed(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        from backend.api import app as studio_app, graphs as graph_store, runner, batch
        monkeypatch.setattr(batch, "BATCHES_DIR", tmp_path / "batches")
        batch._batches.clear()
        g = graph({"code": RATE, "timing": "batch", "metric": "completion_rate"})
        g.update(id="g1", name="g1")
        monkeypatch.setattr(graph_store, "load_graph", lambda gid: g if gid == "g1" else None)
        batch._persist_batch({"batch_id": "b1", "graph_id": "g1", "status": "succeeded", "created_at": "t",
                              "source": {}, "total": 2, "items": [
                                  {"index": 0, "status": "success", "run_id": "r1", "inputs": {}},
                                  {"index": 1, "status": "failed", "run_id": "r2", "inputs": {}}]})
        runs = {"r1": {**RUN, "graph_id": "g1"}, "r2": {**RUN, "run_id": "r2", "status": "failed", "graph_id": "g1"}}
        monkeypatch.setattr(runner, "get_run", lambda rid: runs.get(rid))
        c = TestClient(studio_app.app)
        res = c.post("/api/graphs/g1/evaluators/run", json={"batch_id": "b1"})
        assert res.status_code == 200, res.text
        assert res.json()["evaluations"]["check"]["metrics"]["completion_rate"] == 0.5
        assert batch.get_batch("b1")["evaluations"]["check"]["metrics"]["completion_rate"] == 0.5
        row = next(b for b in batch.list_batches("g1") if b["batch_id"] == "b1")
        assert row["evaluation"] == "check · completion_rate 0.5"

    def test_the_old_post_hoc_routes_are_gone(self):
        from fastapi.testclient import TestClient
        from backend.api import app as studio_app
        c = TestClient(studio_app.app)
        assert c.post("/api/batches/b1/evaluate").status_code in (404, 405)
        assert c.get("/api/batches/b1/evaluation").status_code in (404, 405)
        # The canvas-era routes, too.
        assert c.post("/api/evaluators/graphs/g1/saved", json={}).status_code in (404, 405)
        assert c.post("/api/evaluators/graphs/g1/preview", json={}).status_code in (404, 405)


def test_a_misconfigured_evaluator_reports_itself_and_never_raises():
    """A bad evaluator must not turn a successful run into a failed one: the
    runner calls evaluate_runs on the success path."""
    doc = {"tasks": [], "edges": [], "evaluators": [
        {"name": "bad", "code": "x = (", "timing": "run"},
        {"name": "empty", "code": "", "timing": "run"}]}
    reports = tools.evaluate_runs(doc, [{"run_id": "r1", "status": "success", "inputs": {}, "result": {}}])
    assert reports["bad"]["status"] == "failed" and "syntax error" in reports["bad"]["error"]
    assert reports["empty"]["status"] == "failed" and "no code yet" in reports["empty"]["error"]
