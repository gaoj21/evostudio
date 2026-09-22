"""Stop reaches every layer that can still be working after the click.

One class per thing the user can stop. Each starts real work (a background
thread, a scheduler loop, a watcher poll), holds it on an event, presses Stop,
and asserts what must no longer happen: no further model call, no further run,
no result applied, and a settled status the UI can show.

No provider is ever reached: the model call is a fake that blocks on an event.
"""

import json
import threading
import time

import pytest

evolve_api = pytest.importorskip("backend.api.evolve_api")


def waited(event, timeout=30):
    assert event.wait(timeout), "the work under test never got started"


def settle(task_id, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = evolve_api.get_task(task_id)
        if task and task["status"] != "running":
            return task
        time.sleep(0.02)
    pytest.fail("Evolve task did not settle after Stop")


@pytest.fixture
def evolve(tmp_path, monkeypatch):
    monkeypatch.setattr(evolve_api, "EVOLVE_DIR", tmp_path / "evolve")
    monkeypatch.setattr(evolve_api, "_tasks", {})
    return evolve_api


GRAPH = {"id": "g-stop", "tasks": [{"name": "a", "prompt": "Answer {q}", "kind": "llm"}], "edges": []}
ROWS = [{"q": "one"}, {"q": "two"}]


class TestStoppingASavedResultProposal:
    """The model call that proposes new prompts is already sent and cannot be
    recalled, but what it returns must not be applied."""

    def test_a_proposal_in_flight_ends_the_task_stopped(self, evolve, monkeypatch):
        from backend.api import runner
        from backend.api import saved_result_evolution as saved
        proposing, go = threading.Event(), threading.Event()

        def propose(graph, records, chosen, llm, **kw):
            proposing.set()
            waited(go)                      # the user presses Stop in here
            return ({**graph, "tasks": [{"name": "a", "prompt": "better"}]},
                    [{"name": "a", "before": "Answer {q}", "after": "better", "changed": True}], 2)

        monkeypatch.setattr(runner, "_make_llm", lambda *a, **kw: object())
        monkeypatch.setattr(saved, "evaluate", lambda *a, **kw: {"metrics": {"score": 0.5}, "records": {}})
        monkeypatch.setattr(saved, "propose", propose)

        params = {"mode": "evolve_evaluate", "nodes": ["a"], "n_dev": 2, "n_train": 0,
                  "source": {"type": "saved_run", "run_id": "r1"}}
        task_id = evolve.start_evolve(GRAPH, ROWS, "exact_match", params)
        waited(proposing)
        evolve.stop_evolve_task(task_id)
        go.set()

        task = settle(task_id)
        assert task["status"] == "stopped", "a stopped task reported itself finished"
        assert not task.get("optimized_graph"), "prompts proposed after Stop were applied"
        assert task["error"] is None
        persisted = json.loads((evolve._task_dir(task_id) / "result.json").read_text())
        assert persisted["status"] == "stopped"


class TestStoppingACanvasCandidateRound:
    def test_the_candidate_proposed_after_stop_is_not_kept(self, evolve, monkeypatch):
        from backend.api import runner
        from backend.features.evaluation import canvas_evolution
        from backend.api import saved_result_evolution as saved
        proposing, go = threading.Event(), threading.Event()
        runs = []

        def propose(graph, records, chosen, llm, **kw):
            proposing.set()
            waited(go)
            better = {**graph, "tasks": [{**graph["tasks"][0], "prompt": "candidate"}]}
            return better, [{"name": "a", "before": "Answer {q}", "after": "candidate", "changed": True}], 1

        # A workflow with files of its own: the candidates work on a copy.
        from backend.api import workspace
        real = workspace.files_dir(GRAPH["id"])
        real.mkdir(parents=True, exist_ok=True)
        (real / "notes.txt").write_text("the user's file")

        monkeypatch.setattr(runner, "_make_llm", lambda *a, **kw: object())
        monkeypatch.setattr(runner, "start_run",
                            lambda graph, record, background, run_id, **kw: (runs.append(record), run_id)[1])
        monkeypatch.setattr(runner, "get_run", lambda run_id: {"status": "success", "result": "ok"})
        monkeypatch.setattr(runner, "cancel_run", lambda run_id, **kw: None)
        monkeypatch.setattr(canvas_evolution, "score_saved", lambda *a: {
            "metrics": {"score": 0.5}, "records": {}, "evaluations": {"ev": {"coverage": {}}},
            "objective": {"metric": "score", "direction": "maximize"}})
        monkeypatch.setattr(saved, "propose", propose)

        params = {"mode": "evolve_evaluate", "evaluator": "ev", "nodes": ["a"], "rounds": 2,
                  "n_dev": 2, "n_train": 0, "source": {"type": "canvas", "node": "feed", "config": {}}}
        task_id = evolve.start_evolve(GRAPH, ROWS, "canvas:ev", params)
        waited(proposing)
        started = len(runs)
        evolve.stop_evolve_task(task_id)
        go.set()

        task = settle(task_id)
        assert task["status"] == "stopped"
        assert len(runs) == started, "records ran after Stop"
        assert not task.get("optimized_graph")
        # The scratch stores of every candidate are gone, however it ended:
        # their memory, tables and the copy of the workflow's files.
        assert canvas_evolution.discard_candidate_stores(task_id) == []
        from backend.api import workspace
        assert not workspace.files_dir(f'{canvas_evolution.CANDIDATE_PREFIX}{task_id}-files').exists()
        assert workspace.files_dir(GRAPH["id"]).is_dir(), "the workflow's own files are untouched"


class TestStoppingMipro:
    """dspy calls the model itself, so Stop has to be visible from inside it."""

    def test_the_optimizer_s_own_model_call_raises_once_stopped(self, evolve):
        state = {"task_id": "t", "stop_requested": False}
        model = type("Model", (), {"generate": lambda self, **kw: type("R", (), {"content": "ok"})()})()
        wrapper = evolve._MiproLMWrapper(model)
        with evolve.stoppable(state):
            assert wrapper.forward(prompt="hello") == ["ok"]
            state["stop_requested"] = True
            with pytest.raises(evolve.EvolveStopped):
                wrapper.forward(prompt="hello")
        # Outside a task the same wrapper is untouched.
        assert wrapper.forward(prompt="hello") == ["ok"]

    def test_a_copy_of_the_wrapper_is_stoppable_too(self, evolve):
        """The proposer varies temperature by copying the model."""
        class Config:
            model = "fake"

        class Model:
            def __init__(self, config=None):
                self.config = config or Config()

            def generate(self, **kwargs):
                return type("R", (), {"content": "ok"})()

        wrapper = evolve._MiproLMWrapper(Model())
        copied = wrapper.copy(temperature=0.9)
        state = {"task_id": "t", "stop_requested": True}
        with evolve.stoppable(state):
            with pytest.raises(evolve.EvolveStopped):
                copied.forward(prompt="hello")

    def test_stop_is_not_swallowed_by_a_per_example_handler(self, evolve):
        """The framework evaluator catches Exception around every example."""
        assert not issubclass(evolve.EvolveStopped, Exception)

    def test_a_stop_during_optimization_ends_the_optimizer_and_settles(self, evolve, monkeypatch, tmp_path):
        """The whole MIPRO task: the optimizer's own model call is where Stop
        lands, it ends `optimize`, and the task settles as stopped with no
        best program taken."""
        from types import SimpleNamespace
        from backend.api import runner, tools_registry
        from evoagentx.agents.agent_manager import AgentManager
        from evoagentx.evaluators import Evaluator
        from evoagentx.optimizers.mipro_optimizer import WorkFlowMiproOptimizer
        records = [{"id": str(i), "inputs": {"text": str(i)}, "label": str(i)} for i in range(3)]
        (tmp_path / "dataset.jsonl").write_text("\n".join(json.dumps(r) for r in records))

        model = SimpleNamespace(config=SimpleNamespace(model="fake"),
                               generate=lambda **kw: SimpleNamespace(content="ok"))
        monkeypatch.setattr(runner, "_make_llm", lambda: model)
        monkeypatch.setattr(AgentManager, "add_agents_from_workflow", lambda *a, **kw: None)
        monkeypatch.setattr(tools_registry, "resolve_tools", lambda *a, **kw: [])
        monkeypatch.setattr(Evaluator, "__init__", lambda self, **kw: setattr(self, "collate_func", kw["collate_func"]))

        def evaluate(self, graph, benchmark, **kw):
            for example in benchmark._dev_data:
                self.collate_func(example)      # as the framework evaluator does
            self._evaluation_records = {}
            return {"score": 1}

        monkeypatch.setattr(Evaluator, "evaluate", evaluate)
        monkeypatch.setattr(WorkFlowMiproOptimizer, "__init__", lambda self, **kw: None)
        state = {"task_id": "mipro", "created_at": evolve._utcnow(), "graph_id": "g"}
        monkeypatch.setitem(evolve._tasks, "mipro", state)
        monkeypatch.setattr(evolve, "_task_dir", lambda _: tmp_path)

        def optimize(self, dataset, metric_name):
            # What dspy does between evaluations: its own model calls, through
            # the wrapper. The user presses Stop during the first one.
            wrapper = evolve._MiproLMWrapper(model)
            state["stop_requested"] = True
            wrapper.forward(prompt="propose an instruction")
            raise AssertionError("optimize continued past Stop")

        monkeypatch.setattr(WorkFlowMiproOptimizer, "optimize", optimize)
        graph = {"id": "g", "goal": "test", "tasks": [{"name": "a", "description": "Evaluate text",
                 "prompt": "Original prompt", "inputs": [{"name": "text", "type": "string"}],
                 "outputs": [{"name": "answer", "type": "string"}]}], "edges": []}
        params = evolve.resolve_params({"mode": "evolve_evaluate", "n_train": 0, "n_dev": 3}, 3, ["a"])
        evolve._execute_evolve("mipro", graph, "exact_match", params, tmp_path)

        assert state["status"] == "stopped" and state["error"] is None
        assert not state.get("optimized_graph") and state.get("optimized") is None
        assert state["stage"] is None and state["finished_at"]


class TestStoppingAServerSideTurn:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from backend.api import app
        from backend.features.chat import turn_jobs
        turn_jobs._turns.clear()
        return TestClient(app.app)

    def test_a_stopped_turn_is_not_offered_to_the_poller_again(self, client, monkeypatch):
        """The chat re-attaches to whatever the server reports as running. A
        turn the user stopped must not be in that list, or reopening the chat
        starts watching it again."""
        from backend.features.chat import chat_api
        working, go = threading.Event(), threading.Event()

        def dispatch(graph_id, body):
            working.set()
            waited(go)
            return {"reply": "an answer nobody asked for any more", "operations": []}

        monkeypatch.setattr(chat_api, "_dispatch", dispatch)
        started = client.post("/api/graphs/g-stop/assistant",
                              json={"background": True, "request_id": "req-1", "message": "hi"})
        assert started.status_code == 200
        turn_id = started.json()["turn_id"]
        waited(working)

        stopped = client.post("/api/graphs/g-stop/assistant/req-1/stop")
        assert stopped.status_code == 200
        running = client.get("/api/graphs/g-stop/assistant/turns").json()["turns"]
        assert [t["turn_id"] for t in running] == [], "the poller would re-attach to a stopped turn"

        view = client.get(f"/api/graphs/g-stop/assistant/turns/{turn_id}").json()
        assert view["status"] == "cancelled", "Stop left the turn reporting itself as running"
        assert view["result"]["stopped"] is True
        go.set()
        # Whatever the work returns afterwards is not the turn's answer.
        deadline = time.time() + 10
        while time.time() < deadline and client.get(
                f"/api/graphs/g-stop/assistant/turns/{turn_id}").json()["status"] == "cancelled":
            time.sleep(0.05)
        final = client.get(f"/api/graphs/g-stop/assistant/turns/{turn_id}").json()
        assert final["status"] == "cancelled" and "nobody asked" not in json.dumps(final)


class TestStoppingAResultComputation:
    """A result question can be running Python in a sandboxed subprocess.
    Stop has to end that process, not wait out its time limit."""

    def test_the_sandbox_subprocess_is_killed_and_the_turn_unwinds(self, monkeypatch):
        import sys
        from backend.api import chat_control
        from backend.features.chat import result_compute
        if sys.platform != "darwin":
            pytest.skip("the result sandbox needs sandbox-exec")
        started = []
        real_popen = result_compute.subprocess.Popen

        def popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            started.append(process)
            return process

        monkeypatch.setattr(result_compute.subprocess, "Popen", popen)
        control = chat_control.Control()
        outcome = {}

        def work():
            chat_control.current.set(control)
            try:
                outcome["value"] = result_compute.compute(
                    [], 'import time\nprint("started", flush=True)\ntime.sleep(60)\n')
            except chat_control.Cancelled:
                outcome["stopped"] = True

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        deadline = time.time() + 30
        while not started and time.time() < deadline:
            time.sleep(0.05)
        assert started, "the sandbox never started"
        control.event.set()

        thread.join(20)
        assert not thread.is_alive(), "the question kept waiting for its computation"
        assert outcome.get("stopped") and "value" not in outcome
        assert started[0].poll() is not None, "the sandbox subprocess outlived Stop"


class TestStoppingAnAgentSession:
    @pytest.fixture
    def agent(self, tmp_path, monkeypatch):
        from backend.api import harness_api
        monkeypatch.setattr(harness_api, "_running", {})
        return harness_api

    def session(self, harness_api, status="running"):
        scope = "g-stop"
        session = {"id": "s1", "created_at": time.time(), "status": status,
                   "messages": [{"role": "user", "content": "hi"}], "events": [], "error": None}
        with harness_api.database() as db:
            harness_api.put(db, "a1", scope, "agent", "", {"id": "a1", "name": "A"})
            harness_api.put(db, "s1", scope, "session", "a1", session)
        return session

    def test_stopping_a_session_whose_worker_is_gone_settles_it(self, agent, monkeypatch):
        """A worker killed with the process leaves 'running' in the database
        and nothing in _running. Stop used to hand that straight back, so the
        button said Stopping… over a session that would never settle."""
        monkeypatch.setattr(agent, "graph_for", lambda graph_id: {"id": graph_id})
        self.session(agent)
        result = agent.stop_session("g-stop", "a1", "s1")
        assert result["status"] not in ("running", "stopping"), result["status"]
        assert result["error"]
        listed = agent.list_sessions("g-stop", "a1")["sessions"]
        assert [s["status"] for s in listed] == [result["status"]]

    def test_a_session_still_running_says_what_cannot_be_stopped(self, agent, monkeypatch):
        monkeypatch.setattr(agent, "graph_for", lambda graph_id: {"id": graph_id})
        self.session(agent)
        event = threading.Event()
        agent._running["s1"] = event
        result = agent.stop_session("g-stop", "a1", "s1")
        assert event.is_set()
        assert result["status"] == "stopping"
        assert result["stop_note"]
        with agent.database() as db:
            assert agent.get(db, "s1", "g-stop", "session", "a1")["status"] == "stopping"


class TestAScheduleAfterStop:
    @pytest.fixture
    def sched(self, tmp_path, monkeypatch):
        from backend.features.execution import scheduler
        monkeypatch.setattr(scheduler, "SCHEDULES_DIR", tmp_path / "schedules")
        monkeypatch.setattr(scheduler, "_threads", {})
        yield scheduler
        scheduler.stop("g-stop")

    def test_the_occurrence_being_claimed_does_not_launch(self, sched, monkeypatch):
        """Stop arrives while _fire is already inside its claim. It used to
        launch anyway: the schedule was paused and a run started regardless."""
        from backend.api import graphs as graph_store
        claiming, go = threading.Event(), threading.Event()
        launched = []

        def load_graph(graph_id):
            claiming.set()
            waited(go)
            return {"id": graph_id, "tasks": [], "edges": []}

        monkeypatch.setattr(graph_store, "load_graph", load_graph)
        monkeypatch.setattr("backend.features.execution.scheduled_execution.execution_kind", lambda g: "run")
        monkeypatch.setattr("backend.features.execution.scheduled_execution.launch",
                            lambda *a, **kw: (launched.append(a), "r1")[1])
        schedule = {"enabled": True, "mode": "interval", "interval_minutes": 5, "inputs": {},
                    "recovery_policy": "skip", "session": "s", "experiment_id": "e",
                    "next_fire": "2020-01-01T00:00:00+00:00",
                    "execution_snapshot": {"graph": {"id": "g-stop", "tasks": [], "edges": []}}}
        sched._save("g-stop", schedule)

        stop_event = threading.Event()
        worker = threading.Thread(target=sched._fire, args=("g-stop", schedule, stop_event), daemon=True)
        worker.start()
        waited(claiming)
        stop_event.set()
        go.set()
        worker.join(15)

        assert launched == [], "a scheduled run started after Stop"
        assert sched.load("g-stop").get("in_flight") is None

    def test_a_paused_schedule_never_fires_again(self, sched, monkeypatch):
        fires = []
        monkeypatch.setattr(sched, "_fire", lambda *a, **kw: fires.append(1))
        schedule = {"enabled": False, "mode": "interval", "interval_minutes": 5,
                    "next_fire": "2020-01-01T00:00:00+00:00"}
        sched._save("g-stop", schedule)
        event = threading.Event()
        sched._loop("g-stop", event)
        assert fires == []


class TestStoppingAWatcher:
    @pytest.fixture
    def watch(self, tmp_path, monkeypatch):
        from backend.features.execution import watcher
        monkeypatch.setattr(watcher, "WATCH_DIR", tmp_path / "watch")
        monkeypatch.setattr(watcher, "_watchers", {})
        monkeypatch.setenv("EAX_WATCH_DEBUG", "1")
        yield watcher
        watcher.stop_graph_watch("g-stop")

    def graph(self):
        return {"id": "g-stop", "edges": [{"source": "src", "target": "n"}],
                "tasks": [{"name": "src", "kind": "source", "source": {
                    "type": "plugin_type",
                    "schedule": {"mode": "interval", "interval_minutes": 0.2}}}]}

    def test_a_poll_walking_its_records_gives_up_when_stopped(self, watch, monkeypatch):
        """A poll is not one call: a source type fetches record after record
        (EDGAR pauses between filings). Stop has to reach inside it, or the
        watcher keeps fetching long after the UI says it is gone."""
        from backend.features import plugins
        assert hasattr(watch, "check_stop"), "a poll has no way to notice Stop"
        polling, go = threading.Event(), threading.Event()
        fetched = []

        def records_from_source_node(node):
            polling.set()
            waited(go)
            for index in range(200):
                watch.check_stop()
                fetched.append(index)
            return [{"id": index} for index in range(200)]

        monkeypatch.setattr(plugins, "source_type", lambda t: {"watch_key": "id"})
        monkeypatch.setattr(watch.sources, "records_from_source_node", records_from_source_node)
        monkeypatch.setattr(watch.sources, "find_source_nodes", lambda g: [g["tasks"][0]])
        monkeypatch.setattr(watch.runner, "start_run", lambda *a, **kw: "r")

        assert watch.start_graph_watch(self.graph())
        waited(polling)
        assert watch.stop_graph_watch("g-stop") == 1
        go.set()
        time.sleep(1)
        assert len(fetched) <= 1, f"the poll kept fetching after Stop ({len(fetched)} records)"
