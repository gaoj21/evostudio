"""Phase 3 of RUN_INPUT_FLOW_PLAN.md: the canvas is the execution plan.

Three rules used to decide what ran and what fed what — the drawn edges, the
backend's input derivation, and the framework's own inference from fields
that happened to share a name. They disagreed in ways nobody could see: an
edge between mismatched fields carried nothing, two nodes with a field in
common were wired whether or not anyone drew a line, every tool ran before
every LLM node wherever it sat on the canvas.

Now an edge carries explicit field mappings, a node has an explicit `enabled`
flag, and one compiled DAG is what the plan shows and what the runner walks —
source, tool and LLM nodes alike, in topological order. Written failing.
"""

import pytest

from conftest import make_graph, make_task


# --------------------------------------------------------------------------
# graph shapes
# --------------------------------------------------------------------------

def llm(name, inputs=(), outputs=()):
    return make_task(name, inputs=list(inputs), outputs=list(outputs))


def tool(name, inputs=(), outputs=(), tool_name="shout"):
    t = make_task(name, inputs=list(inputs), outputs=list(outputs))
    t["kind"] = "tool"
    t["tool"] = tool_name
    return t


def source(name, outputs=("company", "news")):
    t = make_task(name, outputs=list(outputs))
    t["kind"] = "source"
    t["source"] = {"type": "credit_risk", "split": "test", "n": 1, "seed": 1}
    return t


def edge(src, dst, mappings=None, control_only=False):
    e = {"source": src, "target": dst}
    if mappings is not None:
        e["mappings"] = [{"from": a, "to": b} for a, b in mappings]
    if control_only:
        e["control_only"] = True
    return e


def graph(tasks, edges):
    g = make_graph(tasks)
    g["edges"] = edges
    g["flow_version"] = 2          # already migrated: mappings are explicit
    return g


@pytest.fixture
def fake_tools(monkeypatch):
    """A tool called `shout` that upper-cases, and one that returns a dict."""
    from backend.api import tools_registry

    calls = []

    def call_tool(name, args, **kw):
        calls.append((name, dict(args)))
        if name == "shout":
            return str(next(iter(args.values()), "")).upper()
        if name == "split":
            text = str(next(iter(args.values()), ""))
            return {"head": text[:1], "tail": text[1:]}
        raise AssertionError(f"unknown fake tool {name}")

    monkeypatch.setattr(tools_registry, "call_tool", call_tool)
    # The registry answers with the toolkit that holds the tool; the export
    # asks for that pair, validation only asks whether it is None.
    monkeypatch.setattr(tools_registry, "find_tool",
                        lambda name: (f"{name}_kit", name) if name in ("shout", "split") else None)
    monkeypatch.setattr(tools_registry, "validate_tool_names", lambda names: None)
    monkeypatch.setattr(tools_registry, "resolve_tools", lambda names, **kw: None)
    return calls


def compile_(g, **kw):
    from backend.api import run_plan
    return run_plan.compile_plan(g, **kw)


def bindings_of(plan, node):
    return next(n for n in plan["nodes"] if n["name"] == node)["input_bindings"]


class TestEdgesCarryData:
    def test_an_edge_between_same_named_fields_binds_them(self):
        p = compile_(graph([llm("a", outputs=["x"]), llm("b", inputs=["x"], outputs=["y"])],
                           [edge("a", "b", [("x", "x")])]))
        assert bindings_of(p, "b") == {"x": {"node": "a", "field": "x"}}
        assert [i["name"] for i in p["inputs"]] == []

    def test_an_edge_may_rename_across_it(self):
        p = compile_(graph([llm("a", outputs=["finding"]), llm("b", inputs=["evidence"], outputs=["y"])],
                           [edge("a", "b", [("finding", "evidence")])]))
        assert bindings_of(p, "b") == {"evidence": {"node": "a", "field": "finding"}}

    def test_an_edge_with_no_mapping_is_refused(self):
        from backend.api import graphs as graph_store
        with pytest.raises(graph_store.GraphValidationError) as err:
            compile_(graph([llm("a", outputs=["x"]), llm("b", inputs=["z"], outputs=["y"])],
                           [edge("a", "b", [])]))
        assert "mapping" in str(err.value.errors).lower()

    def test_a_mapping_must_name_real_fields(self):
        from backend.api import graphs as graph_store
        with pytest.raises(graph_store.GraphValidationError):
            compile_(graph([llm("a", outputs=["x"]), llm("b", inputs=["z"], outputs=["y"])],
                           [edge("a", "b", [("x", "nope")])]))

    def test_no_edge_means_no_dependency_whatever_the_names(self):
        # Two nodes sharing a field name used to be wired by the framework
        # whether or not anyone drew a line.
        p = compile_(graph([llm("a", outputs=["x"]), llm("b", inputs=["x"], outputs=["y"])], []))
        assert bindings_of(p, "b") == {}
        assert [i["name"] for i in p["inputs"]] == ["x"]     # asked for, not inferred

    def test_a_required_input_with_two_producers_is_refused(self):
        from backend.api import graphs as graph_store
        with pytest.raises(graph_store.GraphValidationError) as err:
            compile_(graph([llm("a", outputs=["x"]), llm("b", outputs=["x"]),
                            llm("c", inputs=["x"], outputs=["y"])],
                           [edge("a", "c", [("x", "x")]), edge("b", "c", [("x", "x")])]))
        assert "two" in str(err.value.errors).lower() or "producers" in str(err.value.errors).lower()

    def test_a_control_only_edge_orders_without_carrying(self):
        p = compile_(graph([llm("a", outputs=["x"]), llm("b", inputs=["z"], outputs=["y"])],
                           [edge("a", "b", control_only=True)]))
        node_b = next(n for n in p["nodes"] if n["name"] == "b")
        assert node_b["depends_on"] == ["a"]
        assert node_b["input_bindings"] == {}
        assert [i["name"] for i in p["inputs"]] == ["z"]


class TestEveryKindOfNodeIsOrderedTogether:
    def test_source_to_llm(self, fake_tools):
        p = compile_(graph([source("feed"), llm("judge", inputs=["company"], outputs=["v"])],
                           [edge("feed", "judge", [("company", "company")])]))
        assert bindings_of(p, "judge") == {"company": {"node": "feed", "field": "company"}}

    def test_source_to_tool_to_llm(self, fake_tools):
        p = compile_(graph([source("feed"), tool("loud", inputs=["company"], outputs=["shouted"]),
                            llm("judge", inputs=["shouted"], outputs=["v"])],
                           [edge("feed", "loud", [("company", "company")]),
                            edge("loud", "judge", [("shouted", "shouted")])]))
        assert [n["name"] for n in p["nodes"]] == ["feed", "loud", "judge"]

    def test_llm_to_tool_to_llm_is_allowed_now(self, fake_tools):
        # v1 ran every tool before every LLM node and refused this shape.
        p = compile_(graph([llm("draft", inputs=["topic"], outputs=["text"]),
                            tool("loud", inputs=["text"], outputs=["shouted"]),
                            llm("polish", inputs=["shouted"], outputs=["final"])],
                           [edge("draft", "loud", [("text", "text")]),
                            edge("loud", "polish", [("shouted", "shouted")])]))
        assert [n["name"] for n in p["nodes"]] == ["draft", "loud", "polish"]

    def test_two_branches_join(self):
        p = compile_(graph([llm("a", inputs=["t"], outputs=["x"]), llm("b", inputs=["t"], outputs=["y"]),
                            llm("join", inputs=["x", "y"], outputs=["z"])],
                           [edge("a", "join", [("x", "x")]), edge("b", "join", [("y", "y")])]))
        assert set(bindings_of(p, "join")) == {"x", "y"}
        assert [i["name"] for i in p["inputs"]] == ["t"]


class TestEnabledIsExplicit:
    def test_a_disabled_node_is_left_out_with_its_edges(self):
        off = llm("b", inputs=["x"], outputs=["y"])
        off["enabled"] = False
        p = compile_(graph([llm("a", outputs=["x"]), off, llm("c", inputs=["x"], outputs=["w"])],
                           [edge("a", "b", [("x", "x")]), edge("a", "c", [("x", "x")])]))
        assert [n["name"] for n in p["nodes"]] == ["a", "c"]
        assert "b" in p["skipped"]

    def test_an_unconnected_node_still_runs_when_enabled(self):
        # The old rule parked any edgeless node the moment the canvas had an
        # unrelated edge somewhere else. Two independent subgraphs both run.
        p = compile_(graph([llm("a", outputs=["x"]), llm("b", inputs=["x"], outputs=["y"]),
                            llm("alone", inputs=["q"], outputs=["r"])],
                           [edge("a", "b", [("x", "x")])]))
        assert [n["name"] for n in p["nodes"]] == ["a", "b", "alone"]
        assert [i["name"] for i in p["inputs"]] == ["q"]


class TestOldGraphsAreMigratedOnce:
    def test_a_lone_same_name_pair_becomes_the_mapping(self):
        from backend.api import graphs as graph_store
        old = make_graph([llm("a", outputs=["x"]), llm("b", inputs=["x"], outputs=["y"])],
                         edges=[("a", "b")])
        new = graph_store.migrate_flow(old)
        assert new["edges"][0]["mappings"] == [{"from": "x", "to": "x"}]
        assert new["flow_version"] == graph_store.FLOW_VERSION

    def test_an_edge_with_nothing_in_common_becomes_control_only(self):
        from backend.api import graphs as graph_store
        old = make_graph([llm("a", outputs=["x"]), llm("b", inputs=["z"], outputs=["y"])],
                         edges=[("a", "b")])
        new = graph_store.migrate_flow(old)
        assert new["edges"][0]["control_only"] is True
        assert any("control" in w for w in new.get("migration_warnings", []))

    def test_what_was_parked_becomes_disabled(self):
        from backend.api import graphs as graph_store
        old = make_graph([llm("a", outputs=["x"]), llm("b", inputs=["x"], outputs=["y"]),
                          llm("draft", inputs=["q"], outputs=["r"])],
                         edges=[("a", "b")])
        new = graph_store.migrate_flow(old)
        by = {t["name"]: t for t in new["tasks"]}
        assert by["draft"]["enabled"] is False
        assert by["a"].get("enabled", True) is True

    def test_a_read_without_an_edge_becomes_an_edge(self):
        # The framework let `decide` read `company` from `feed` with no line
        # between them. The migration draws that line rather than turning a
        # working workflow into one that asks for `company` every run.
        from backend.api import graphs as graph_store
        old = make_graph([source("feed"), llm("detect", inputs=["company"], outputs=["detection"]),
                          llm("decide", inputs=["company", "detection"], outputs=["decision"])],
                         edges=[("feed", "detect"), ("detect", "decide")])
        new = graph_store.migrate_flow(old)
        pairs = {(e["source"], e["target"]): e for e in new["edges"]}
        assert pairs[("feed", "decide")]["mappings"] == [{"from": "company", "to": "company"}]
        assert any("Added edge feed → decide" in w for w in new["migration_warnings"])
        from backend.api import run_plan
        assert [i["name"] for i in run_plan.compile_plan(new)["inputs"]] == []

    def test_a_read_with_two_possible_producers_is_flagged_not_guessed(self):
        # Two earlier nodes both produce `note`; neither is adjacent to the
        # reader. An adjacent same-name edge would have settled it — here
        # nothing does, so nothing is drawn and the plan asks instead.
        from backend.api import graphs as graph_store
        old = make_graph([llm("a", inputs=["t"], outputs=["note"]), llm("b", inputs=["t"], outputs=["note"]),
                          llm("x", inputs=["t"], outputs=["w"]), llm("c", inputs=["note", "w"], outputs=["z"])],
                         edges=[("a", "x"), ("b", "x"), ("x", "c")])
        new = graph_store.migrate_flow(old)
        assert not any(e.get("target") == "c" and e.get("source") in ("a", "b") for e in new["edges"])
        assert any("'c.note'" in w and "asked for" in w for w in new["migration_warnings"])
        from backend.api import run_plan
        assert "note" in [i["name"] for i in run_plan.compile_plan(new)["inputs"]]

    def test_a_migrated_graph_is_not_migrated_again(self):
        from backend.api import graphs as graph_store
        g = graph([llm("a", outputs=["x"]), llm("b", inputs=["z"], outputs=["y"])],
                  [edge("a", "b", control_only=True)])
        assert graph_store.migrate_flow(g) == g

    def test_loading_a_saved_old_graph_migrates_it(self, studio_data):
        from backend.api import graphs as graph_store
        created = graph_store.create_graph("Old", "g")
        old = make_graph([llm("a", outputs=["x"]), llm("b", inputs=["x"], outputs=["y"])],
                         edges=[("a", "b")])
        old.pop("flow_version", None)
        # A save follows the body's name, so the id can change under it.
        saved = graph_store.save_graph(created["id"], old)
        loaded = graph_store.load_graph(saved["id"])
        assert loaded["flow_version"] == graph_store.FLOW_VERSION
        assert loaded["edges"][0]["mappings"] == [{"from": "x", "to": "x"}]


# --------------------------------------------------------------------------
# execution — the fixed end-to-end flows from §5.4, without a paid model
# --------------------------------------------------------------------------

@pytest.fixture
def engine(studio_data, monkeypatch, fake_tools):
    """The runner with the model swapped for a recorder.

    Every LLM node returns `<name>(<its inputs>)` so the test can see exactly
    what each node was handed and in what order they ran.
    """
    from evoagentx.models import LiteLLMConfig
    from backend.api import runner

    seen = []

    async def fake_llm(agent, task, inputs, state):
        seen.append((task["name"], dict(inputs)))
        return {o["name"]: f"{task['name']}({','.join(f'{k}={v}' for k, v in sorted(inputs.items()))})"
                for o in task.get("outputs") or []}

    class StubLLM:
        config = LiteLLMConfig(model="deepseek/deepseek-chat", deepseek_key="test-only")

    monkeypatch.setattr(runner, "execute_llm_node", fake_llm)
    monkeypatch.setattr(runner, "_make_llm", lambda **kw: StubLLM())
    monkeypatch.setattr(runner, "_prepare_ltm", lambda doc, ordered, inputs, state: ({}, ordered))
    monkeypatch.setattr(runner, "_attach_ltm", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_save_ltm", lambda *a, **k: None)

    def run(g, inputs=None, **kw):
        g = {**g, "id": "flow-e2e"}
        run_id = runner.start_run(g, inputs or {}, background=False, **kw)
        return runner.get_run(run_id)

    run.seen = seen
    run.tools = fake_tools
    return run


def node_status(run, name):
    return next(n["status"] for n in run["nodes"] if n["name"] == name)


class TestFixedFlows:
    def test_input_to_tool_to_output(self, engine):
        g = graph([tool("loud", inputs=["text"], outputs=["shouted"])], [])
        run = engine(g, {"text": "hi"})

        assert run["status"] == "success", run.get("error")
        assert run["result"] == {"shouted": "HI"}
        assert engine.tools == [("shout", {"text": "hi"})]

    def test_input_to_llm_to_tool_to_llm_runs_in_canvas_order(self, engine):
        g = graph([llm("draft", inputs=["topic"], outputs=["text"]),
                   tool("loud", inputs=["text"], outputs=["shouted"]),
                   llm("polish", inputs=["shouted"], outputs=["final"])],
                  [edge("draft", "loud", [("text", "text")]),
                   edge("loud", "polish", [("shouted", "shouted")])])
        run = engine(g, {"topic": "rates"})

        assert run["status"] == "success", run.get("error")
        assert [name for name, _ in engine.seen] == ["draft", "polish"]
        assert engine.tools == [("shout", {"text": "draft(topic=rates)"})]
        assert engine.seen[1][1] == {"shouted": "DRAFT(TOPIC=RATES)"}
        assert run["result"] == {"final": "polish(shouted=DRAFT(TOPIC=RATES))"}
        assert node_status(run, "loud") == "completed"

    def test_source_to_two_branches_to_join(self, engine, monkeypatch):
        from backend.api import sources
        monkeypatch.setattr(sources, "records_from_source_node",
                            lambda node: [{"company": "Acme", "news": "n1"}])
        g = graph([source("feed"),
                   llm("a", inputs=["company"], outputs=["x"]),
                   llm("b", inputs=["news"], outputs=["y"]),
                   llm("join", inputs=["x", "y"], outputs=["z"])],
                  [edge("feed", "a", [("company", "company")]),
                   edge("feed", "b", [("news", "news")]),
                   edge("a", "join", [("x", "x")]),
                   edge("b", "join", [("y", "y")])])
        run = engine(g)

        assert run["status"] == "success", run.get("error")
        got = dict(engine.seen)
        assert got["a"] == {"company": "Acme"}          # only its own edge
        assert got["b"] == {"news": "n1"}               # not the sibling's field
        assert got["join"] == {"x": "a(company=Acme)", "y": "b(news=n1)"}
        assert engine.seen[-1][0] == "join"

    def test_a_field_with_the_same_name_does_not_leak_across_branches(self, engine):
        # `note` exists on both sides; nothing connects them.
        g = graph([llm("left", inputs=["t"], outputs=["note"]),
                   llm("right", inputs=["t", "note"], outputs=["out"])],
                  [])
        run = engine(g, {"t": "x", "note": "supplied"})

        assert run["status"] == "success", run.get("error")
        assert dict(engine.seen)["right"] == {"t": "x", "note": "supplied"}

    def test_a_tool_with_several_outputs_is_split_by_key(self, engine):
        g = graph([tool("cut", inputs=["text"], outputs=["head", "tail"], tool_name="split"),
                   llm("use", inputs=["head", "tail"], outputs=["o"])],
                  [edge("cut", "use", [("head", "head"), ("tail", "tail")])])
        run = engine(g, {"text": "abc"})

        assert run["status"] == "success", run.get("error")
        assert dict(engine.seen)["use"] == {"head": "a", "tail": "bc"}

    def test_a_tool_missing_a_declared_output_fails_naming_it(self, engine):
        g = graph([tool("cut", inputs=["text"], outputs=["head", "middle"], tool_name="split")], [])
        run = engine(g, {"text": "abc"})

        assert run["status"] == "failed"
        assert "middle" in run["error"]
        assert node_status(run, "cut") == "failed"

    def test_a_disabled_node_is_reported_skipped_not_run(self, engine):
        off = llm("extra", inputs=["topic"], outputs=["e"])
        off["enabled"] = False
        g = graph([llm("a", inputs=["topic"], outputs=["x"]), off], [])
        run = engine(g, {"topic": "t"})

        assert run["status"] == "success", run.get("error")
        assert [name for name, _ in engine.seen] == ["a"]
        assert node_status(run, "extra") == "skipped"

    def test_the_plan_and_the_run_agree_on_the_nodes(self, engine):
        from backend.api import run_plan
        g = graph([llm("a", inputs=["topic"], outputs=["x"]),
                   tool("loud", inputs=["x"], outputs=["s"]),
                   llm("alone", inputs=["q"], outputs=["r"])],
                  [edge("a", "loud", [("x", "x")])])
        planned = [n["name"] for n in run_plan.compile_plan(g)["nodes"]]
        run = engine(g, {"topic": "t", "q": "u"})

        executed = [n["name"] for n in run["nodes"] if n["status"] == "completed"]
        assert executed == planned == ["a", "loud", "alone"]


class TestTheExportWalksTheSamePlan:
    """An exported project must not quietly run a different workflow than
    Studio does. It used to run every tool first and let the framework infer
    the rest; now it carries the compiled plan and walks it."""

    @pytest.fixture
    def generated(self, fake_tools):
        from backend.api import export_api
        g = graph([llm("draft", inputs=["topic"], outputs=["text"]),
                   tool("loud", inputs=["text"], outputs=["shouted"]),
                   llm("polish", inputs=["shouted"], outputs=["final"])],
                  [edge("draft", "loud", [("text", "text")]),
                   edge("loud", "polish", [("shouted", "shouted")])])
        g["id"] = "g1"; g["name"] = "G"
        files, _ = export_api.project_files(g, include_vendor=False)
        return files["workflow.py"]

    def test_it_is_valid_python(self, generated):
        import ast
        ast.parse(generated)

    def test_it_carries_the_plan_in_canvas_order(self, generated):
        import ast, re
        plan = ast.literal_eval(re.search(r"PLAN = (\[.*?\])\n", generated, re.S).group(1))
        assert [n["name"] for n in plan] == ["draft", "loud", "polish"]
        assert plan[2]["input_bindings"] == {"shouted": {"node": "loud", "field": "shouted"}}

    def test_tools_are_no_longer_run_ahead_of_everything(self, generated):
        assert "run_tool_nodes(" not in generated
        assert "SequentialWorkFlowGraph(goal=GOAL, tasks=framework_tasks)" not in generated

    def test_a_source_binding_is_read_from_the_inputs(self, fake_tools):
        # An export does not execute a source; its fields are supplied.
        import ast, re
        from backend.api import export_api
        g = graph([source("feed"), llm("judge", inputs=["company"], outputs=["v"])],
                  [edge("feed", "judge", [("company", "company")])])
        g["id"] = "g1"; g["name"] = "G"
        files, _ = export_api.project_files(g, include_vendor=False)
        plan = ast.literal_eval(re.search(r"PLAN = (\[.*?\])\n", files["workflow.py"], re.S).group(1))
        assert [n["name"] for n in plan] == ["judge"]
        assert plan[0]["input_bindings"] == {"company": {"external": "company"}}


class TestNothingIsReadByNameAlone:
    def test_an_unbound_input_is_not_taken_from_whoever_produced_it(self, engine):
        # `left` produces `note`; `right` takes `note`; no edge, nothing
        # supplied. The old engine would have handed left's value across —
        # the inference this phase removes. The run must refuse, naming it.
        g = graph([llm("left", inputs=["t"], outputs=["note"]),
                   llm("right", inputs=["t", "note"], outputs=["out"])],
                  [edge("left", "right", control_only=True)])   # order only
        run = engine(g, {"t": "x"})

        assert run["status"] == "failed"
        assert "note" in run["error"]
        assert [name for name, _ in engine.seen] == ["left"]     # right never ran
        assert node_status(run, "right") == "failed"



class TestASuppliedRecordIsTheRecord:
    """A batch hands each run its record as inputs. The source node must not
    re-sample over it: it did, and thirteen records of a weekly backtest all
    computed the first sample's first week."""

    def test_a_source_does_not_sample_when_its_fields_are_supplied(self, engine, monkeypatch):
        from backend.api import sources
        sampled = []
        monkeypatch.setattr(sources, "records_from_source_node",
                            lambda node: sampled.append(1) or [{"company": "Moderna", "news": "week 1"}])
        g = graph([source("feed"), llm("judge", inputs=["company", "news"], outputs=["v"])],
                  [edge("feed", "judge", [("company", "company"), ("news", "news")])])
        run = engine(g, {"company": "EchoStar", "news": "week 9"})   # a batch record

        assert run["status"] == "success", run.get("error")
        assert sampled == []                                          # never touched the dataset
        assert dict(engine.seen)["judge"] == {"company": "EchoStar", "news": "week 9"}

    def test_a_source_samples_only_what_is_missing(self, engine, monkeypatch):
        from backend.api import sources
        monkeypatch.setattr(sources, "records_from_source_node",
                            lambda node: [{"company": "Moderna", "news": "sampled"}])
        g = graph([source("feed"), llm("judge", inputs=["company", "news"], outputs=["v"])],
                  [edge("feed", "judge", [("company", "company"), ("news", "news")])])
        run = engine(g, {"company": "EchoStar"})                       # half supplied

        assert run["status"] == "success", run.get("error")
        assert dict(engine.seen)["judge"] == {"company": "EchoStar", "news": "sampled"}


def test_tool_only_run_does_not_initialize_a_model(fake_tools, studio_data, monkeypatch):
    from backend.api import runner
    monkeypatch.setattr(runner, 'RUNS_DIR', studio_data/'runs')
    monkeypatch.setattr(runner, '_make_llm', lambda **kw: pytest.fail('Tool-only task initialized a model'))
    g=graph([tool('uppercase',inputs=['text'],outputs=['result'])],[])
    rid=runner.start_run(g,{'text':'hello'},background=False)
    result=runner.get_run(rid)
    assert result['status']=='success',result['error']
    assert result['nodes'][0]['output']['result']=='HELLO'


def test_tool_dependencies_are_checked_only_for_executed_agents(monkeypatch):
    from backend.api import run_plan, tools_registry
    seen=[]
    monkeypatch.setattr(tools_registry,'validate_tool_names',lambda names:seen.extend(names))
    earlier=llm('first',outputs=['value']);earlier['tool_names']=['UnavailableToolkit']
    later=llm('second',inputs=['value'],outputs=['answer'])
    run_plan.compile_plan(graph([earlier,later],[edge('first','second',[('value','value')])]),start_at=['second'])
    assert seen==[]
