"""Memory selection where it actually takes effect.

The policy module decides what to keep; these check that the run engine and
the exported project both ask it, rather than each keeping their own idea of
what a node remembers.
"""

import json

import pytest


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeMemory:
    """Stands in for a LongTermMemory: records what was written to it."""

    def __init__(self, hits=()):
        self.messages = []
        self.saved = 0
        self.searches = []
        self.hits = list(hits)
        self.storage_handler = object()

    def search(self, query, n=3):
        self.searches.append((query, n))
        return self.hits[:n]

    def add(self, messages):
        self.messages.extend(messages)

    def save(self):
        self.saved += 1


class FakeParam:
    def __init__(self, name):
        self.name = name


class FakeNode:
    def __init__(self, name, inputs, outputs):
        self.name = name
        self.inputs = [FakeParam(i) for i in inputs]
        self.outputs = [FakeParam(o) for o in outputs]


class FakeGraph:
    def __init__(self, nodes):
        self.nodes = nodes


class FakeWorkflow:
    def __init__(self, data):
        self.environment = self
        self._data = data

    def get_all_execution_data(self):
        return self._data


class FrameworkShapedMemory(FakeMemory):
    """A store shaped like the framework's LongTermMemory.

    Its synchronous `search` wraps the async one in `asyncio.run()`, which
    raises when a loop is already running — and recall happens inside the
    workflow's loop. Only the async entry point is safe there.
    """

    async def search_async(self, query, n=3):
        self.searches.append((query, n))
        return self.hits[:n]

    def search(self, query, n=3):
        import asyncio

        return asyncio.run(self.search_async(query, n))


def record_prompt(action):
    """Stand in for the action's real execution, noting the prompt it was given.

    The recall is spliced into `action.prompt` for the duration of one call, so
    the only way to see it is from inside that call — and doing so must not
    require an LLM.
    """
    seen = {}

    async def capture(*args, inputs=None, **kwargs):
        seen["prompt"] = action.prompt
        seen["inputs"] = inputs
        return ("result", action.prompt)

    action.async_execute = capture
    return seen


def run_action(action, inputs):
    import asyncio

    return asyncio.run(action.async_execute(inputs=inputs))


def graph_doc(**memory):
    return {
        "id": "g1", "goal": "judge things",
        "tasks": [{
            "name": "judge", "description": "decide", "use_long_term_memory": True,
            "prompt": "Company: {company}\nNews: {news}",
            "inputs": [{"name": "company"}, {"name": "news"}],
            "outputs": [{"name": "verdict"}, {"name": "why"}],
            **({"memory": memory} if memory else {}),
        }],
        "edges": [],
    }


@pytest.fixture
def saved(monkeypatch):
    """Runs _save_ltm against fakes and hands back what reached the store.

    The write goes through a store opened at save time, not the one handed in:
    that is what stops the records of a batch overwriting each other.
    """
    import memory_store
    import runner

    def run(doc, data, succeeded=True):
        memory = FakeMemory()
        monkeypatch.setattr(memory_store, "open_memory",
                            lambda gid, agent, create=False: memory)
        state = {}
        runner._save_ltm(
            doc, {"judge": FakeMemory()},
            FakeGraph([FakeNode("judge", ["company", "news"], ["verdict", "why"])]),
            FakeWorkflow(data), state, succeeded=succeeded,
        )
        assert state.get("memory_error") is None, state.get("memory_error")
        return memory

    return run


DATA = {"company": "Gaucho", "news": "n" * 20_000,
        "verdict": "concern", "why": "a short reason"}


class TestTheRunEngine:
    def test_a_table_only_graph_still_writes(self, tmp_path, monkeypatch):
        import runner
        import table_store

        monkeypatch.setattr(table_store, "TABLES_DIR", tmp_path / "tables")
        doc = graph_doc(kind="table", match="company", at="as_of",
                        inputs=["company", "as_of"], outputs=["verdict"])
        data = {**DATA, "as_of": "2026-01-12"}
        state = {}

        runner._save_ltm(
            doc, {},
            FakeGraph([FakeNode("judge", ["company", "as_of"], ["verdict"])]),
            FakeWorkflow(data), state,
        )

        rows = table_store.rows("g1", "judge", "Gaucho")
        assert len(rows) == 1
        assert rows[0]["payload"]["outputs"] == {"verdict": "concern"}
        assert state["memory_written"][0]["kind"] == "table"

    def test_a_table_node_without_its_key_does_not_create_a_vector_store(
            self, monkeypatch):
        import memory_store
        import runner

        opened = []
        monkeypatch.setattr(memory_store, "open_memory",
                            lambda *a, **k: opened.append((a, k)))
        doc = graph_doc(kind="table", match="company", outputs=["verdict"], inputs=[])
        state = {}

        runner._save_ltm(
            doc, {}, FakeGraph([FakeNode("judge", [], ["verdict"])]),
            FakeWorkflow({"verdict": "concern"}), state,
        )

        assert opened == []
        assert "no table-memory subject" in state["memory_notes"][0]

    def test_it_stores_only_what_the_node_selected(self, saved):
        memory = saved(graph_doc(outputs=["verdict"], inputs=["company"]), DATA)
        entry = json.loads(memory.messages[0].content)

        assert entry["outputs"] == {"verdict": "concern"}
        assert entry["inputs"] == {"company": "Gaucho"}
        assert memory.saved == 1

    def test_a_node_that_selected_nothing_present_writes_nothing(self, saved):
        memory = saved(graph_doc(outputs=["verdict"], inputs=[]),
                       {"company": "Gaucho"})   # the run produced no verdict
        assert memory.messages == []
        assert memory.saved == 0

    def test_the_short_output_survives_a_huge_input(self, saved):
        memory = saved(graph_doc(), DATA)   # no selection: everything, as before
        entry = json.loads(memory.messages[0].content)

        # This is what the old whole-blob truncation lost.
        assert entry["outputs"] == {"verdict": "concern", "why": "a short reason"}

    def test_a_failed_run_writes_nothing_by_default(self, saved):
        memory = saved(graph_doc(outputs=["verdict"]), DATA, succeeded=False)
        assert memory.messages == []

    def test_a_node_can_opt_into_remembering_failures(self, saved):
        memory = saved(graph_doc(when="always", outputs=["verdict"]), DATA,
                       succeeded=False)
        assert json.loads(memory.messages[0].content)["outputs"] == {"verdict": "concern"}

    def test_the_message_carries_the_node_it_came_from(self, saved):
        message = saved(graph_doc(), DATA).messages[0]
        assert message.agent == "judge"
        assert message.wf_task == "judge"
        assert message.wf_task_desc == "decide"

    def test_a_broken_store_does_not_fail_the_run(self, monkeypatch):
        import memory_store
        import runner

        class Exploding(FakeMemory):
            def add(self, messages):
                raise RuntimeError("disk full")

        monkeypatch.setattr(memory_store, "open_memory",
                            lambda gid, agent, create=False: Exploding())
        state = {}
        runner._save_ltm(
            graph_doc(), {"judge": FakeMemory()},
            FakeGraph([FakeNode("judge", ["company"], ["verdict"])]),
            FakeWorkflow(DATA), state,
        )
        # Recorded for the run view, not raised: the run itself succeeded.
        assert "disk full" in state["memory_error"]


class TestRecall:
    """Searching happens when the node runs, not before the workflow starts.

    A node fed by an upstream node is not described by the workflow's inputs at
    all, so a search made before anything had executed was matching on text
    that node would never see.
    """

    @pytest.fixture
    def built(self):
        """A real two-node framework graph and its agents.

        Built for real rather than faked: the two things being checked here are
        properties of the framework's own objects — what it names an agent, and
        when it hands an action its inputs.
        """
        from evoagentx.agents.agent_manager import AgentManager
        from evoagentx.models import LiteLLM, LiteLLMConfig
        from evoagentx.workflow.workflow_graph import SequentialWorkFlowGraph

        tasks = [
            {"name": "judge", "description": "decide",
             "inputs": [{"name": "company", "type": "str", "description": "c",
                         "required": True}],
             "outputs": [{"name": "verdict", "type": "str", "description": "v",
                          "required": True}],
             "prompt": "Company: {company}", "parse_mode": "str"},
            {"name": "report", "description": "write it up",
             "inputs": [{"name": "verdict", "type": "str", "description": "v",
                         "required": True}],
             "outputs": [{"name": "summary", "type": "str", "description": "s",
                          "required": True}],
             "prompt": "Verdict: {verdict}", "parse_mode": "str"},
        ]
        graph = SequentialWorkFlowGraph(goal="g", tasks=tasks)
        manager = AgentManager()
        manager.add_agents_from_workflow(
            graph, llm_config=LiteLLMConfig(model="deepseek/deepseek-chat",
                                            deepseek_key="test-only")
        )
        doc = {"id": "g1", "goal": "g", "tasks": [
            {**t, "use_long_term_memory": True} for t in tasks], "edges": []}
        return graph, manager, doc

    @staticmethod
    def main_action(agent):
        from evoagentx.actions.customize_action import CustomizeAction

        return next(a for a in agent.actions if isinstance(a, CustomizeAction))

    def test_opening_the_stores_does_not_search_them(self, monkeypatch):
        import memory_store
        import runner

        memory = FakeMemory()
        monkeypatch.setattr(memory_store, "open_memory", lambda *a, **k: memory)
        doc = graph_doc()
        memories, tasks = runner._prepare_ltm(doc, doc["tasks"], {"company": "G"}, {})

        assert memories == {"judge": memory}
        assert memory.searches == []
        # Nothing is spliced into the prompt this early either.
        assert tasks[0]["prompt"] == "Company: {company}\nNews: {news}"

    def test_the_store_reaches_the_agent_the_framework_actually_built(self, built):
        import runner

        graph, manager, doc = built
        memory = FakeMemory()
        runner._attach_ltm(manager, graph, doc, {"judge": memory}, {})

        # The framework calls it "JudgeAgent"; looking it up by the task name
        # matched nothing and attached nothing, silently.
        agent = next(a for a in manager.agents if a.name == "JudgeAgent")
        assert agent.long_term_memory is memory
        assert agent.use_long_term_memory is True

    def test_it_searches_with_the_inputs_the_node_was_given(self, built):
        import runner

        graph, manager, doc = built
        memory = FakeMemory()
        runner._attach_ltm(manager, graph, doc, {"report": memory}, {})

        action = self.main_action(
            next(a for a in manager.agents if a.name == "ReportAgent"))
        seen = record_prompt(action)
        runner._attach_ltm(manager, graph, doc, {"report": memory}, {})
        run_action(action, {"verdict": "concern"})

        # "concern" comes from the node upstream — it is not a workflow input
        # and could not have been part of a search made before the run.
        assert memory.searches, "the node never searched its memory"
        assert "concern" in memory.searches[0][0]

    def test_the_recall_goes_into_the_prompt_for_that_call_only(self, built):
        import runner

        graph, manager, doc = built
        memory = FakeMemory()
        memory.hits = [(FakeMessage(json.dumps(
            {"inputs": {"verdict": "concern"}, "outputs": {"summary": "escalated"}}
        )), 0.9)]
        action = self.main_action(next(a for a in manager.agents if a.name == "ReportAgent"))
        base = action.prompt
        seen = record_prompt(action)
        runner._attach_ltm(manager, graph, doc, {"report": memory}, {})
        run_action(action, {"verdict": "concern"})

        assert "Similar past runs" in seen["prompt"]
        assert "verdict: concern → summary: escalated" in seen["prompt"]
        # Put back afterwards: the action is reused, and a prompt that grew by
        # one memory block per call would compound.
        assert action.prompt == base

    def test_braces_in_a_recalled_memory_survive_prompt_formatting(self, built):
        import runner

        graph, manager, doc = built
        memory = FakeMemory()
        memory.hits = [(FakeMessage('{"outputs": {"summary": "a {tricky} value"}}'), 0.9)]
        action = self.main_action(next(a for a in manager.agents if a.name == "ReportAgent"))
        seen = record_prompt(action)
        runner._attach_ltm(manager, graph, doc, {"report": memory}, {})
        run_action(action, {"verdict": "c"})

        # The prompt is a format string; an unescaped brace here is a KeyError
        # at the moment the node runs.
        seen["prompt"].format(verdict="c")

    def test_a_node_can_write_memory_without_reading_it(self, built):
        import runner

        graph, manager, doc = built
        doc["tasks"][1]["memory"] = {"retrieve": 0}
        memory = FakeMemory()
        action = self.main_action(next(a for a in manager.agents if a.name == "ReportAgent"))
        seen = record_prompt(action)
        runner._attach_ltm(manager, graph, doc, {"report": memory}, {})
        run_action(action, {"verdict": "c"})

        assert memory.searches == []
        assert "Similar past runs" not in seen["prompt"]

    def test_a_framework_store_is_searched_without_nesting_event_loops(self, built):
        import runner

        graph, manager, doc = built
        memory = FrameworkShapedMemory(
            hits=[(FakeMessage('{"outputs": {"summary": "escalated"}}'), 0.9)])
        action = self.main_action(next(a for a in manager.agents if a.name == "ReportAgent"))
        seen = record_prompt(action)
        state = {}
        runner._attach_ltm(manager, graph, doc, {"report": memory}, state)
        run_action(action, {"verdict": "c"})

        # Reaching for the synchronous call here raises "asyncio.run() cannot
        # be called from a running event loop", and the node runs with no
        # memory at all while the failure hides in memory_error.
        assert state.get("memory_error") is None, state.get("memory_error")
        assert "summary: escalated" in seen["prompt"]

    def test_a_store_with_only_a_synchronous_search_still_works(self, built):
        import runner

        # The langchain backend has no async entry point at all.
        graph, manager, doc = built
        memory = FakeMemory(hits=[(FakeMessage('{"outputs": {"summary": "held"}}'), 0.9)])
        assert not hasattr(memory, "search_async")
        action = self.main_action(next(a for a in manager.agents if a.name == "ReportAgent"))
        seen = record_prompt(action)
        state = {}
        runner._attach_ltm(manager, graph, doc, {"report": memory}, state)
        run_action(action, {"verdict": "c"})

        assert state.get("memory_error") is None
        assert "summary: held" in seen["prompt"]

    def test_a_store_that_cannot_be_searched_does_not_stop_the_node(self, built):
        import runner

        graph, manager, doc = built

        class Exploding(FakeMemory):
            def search(self, query, n=3):
                raise RuntimeError("index corrupt")

        state = {}
        action = self.main_action(next(a for a in manager.agents if a.name == "ReportAgent"))
        seen = record_prompt(action)
        runner._attach_ltm(manager, graph, doc, {"report": Exploding()}, state)
        run_action(action, {"verdict": "c"})

        # The node still ran, with its own prompt, and the problem was recorded.
        assert seen["prompt"] == "Verdict: {verdict}"
        assert "index corrupt" in state["memory_error"]


class TestReadingAnotherNodesMemory:
    """A node can draw on what another node learned, not only on its own."""

    def test_the_other_node_s_store_is_opened_too(self, monkeypatch):
        import memory_store
        import runner

        opened = {}

        def fake_open(graph_id, agent, create=False):
            opened[agent] = create
            return FakeMemory()

        monkeypatch.setattr(memory_store, "open_memory", fake_open)
        doc = graph_doc(read_from=["judge", "investigate"])
        doc["tasks"].append({"name": "investigate", "description": "look",
                             "inputs": [], "outputs": [{"name": "context"}]})
        runner._prepare_ltm(doc, doc["tasks"], {}, {})

        # Read-only: naming a node must not bring a store into existence for
        # one that never remembers anything.
        assert opened == {"judge": True, "investigate": False}

    def test_a_store_that_does_not_exist_is_simply_absent(self, monkeypatch):
        import memory_store
        import runner

        monkeypatch.setattr(memory_store, "open_memory",
                            lambda gid, agent, create=False:
                            FakeMemory() if create else None)
        doc = graph_doc(read_from=["judge", "never_ran"])
        memories, _ = runner._prepare_ltm(doc, doc["tasks"], {}, {})
        assert list(memories) == ["judge"]

    def test_it_recalls_from_the_store_it_named(self, built):
        import runner

        graph, manager, doc = built
        doc["tasks"][1]["memory"] = {"read_from": ["judge"]}
        theirs = FakeMemory(hits=[(FakeMessage(json.dumps(
            {"outputs": {"verdict": "concern"}})), 0.9)])
        action = self.main_action(next(a for a in manager.agents if a.name == "ReportAgent"))
        seen = record_prompt(action)
        runner._attach_ltm(manager, graph, doc,
                           {"judge": theirs, "report": FakeMemory()}, {})
        run_action(action, {"verdict": "c"})

        assert "verdict: concern" in seen["prompt"]

    def test_another_node_s_memory_says_whose_it_is(self, built):
        import runner

        graph, manager, doc = built
        doc["tasks"][1]["memory"] = {"read_from": ["judge", "report"]}
        action = self.main_action(next(a for a in manager.agents if a.name == "ReportAgent"))
        seen = record_prompt(action)
        runner._attach_ltm(manager, graph, doc, {
            "judge": FakeMemory(hits=[(FakeMessage('{"outputs": {"verdict": "c"}}'), 0.9)]),
            "report": FakeMemory(hits=[(FakeMessage('{"outputs": {"summary": "s"}}'), 0.9)]),
        }, {})
        run_action(action, {"verdict": "c"})

        # Attributed when it is not this node's own; unattributed when it is,
        # where saying so would only be noise.
        assert "- (judge) verdict: c" in seen["prompt"]
        assert "- summary: s" in seen["prompt"]

    def test_it_reads_only_the_fields_it_asked_for(self, built):
        import runner

        graph, manager, doc = built
        doc["tasks"][1]["memory"] = {"read": ["verdict"]}
        action = self.main_action(next(a for a in manager.agents if a.name == "ReportAgent"))
        seen = record_prompt(action)
        runner._attach_ltm(manager, graph, doc, {"report": FakeMemory(hits=[
            (FakeMessage(json.dumps({"inputs": {"company": "Gaucho"},
                                     "outputs": {"verdict": "c",
                                                 "why": "a reason"}})), 0.9),
        ])}, {})
        run_action(action, {"verdict": "c"})

        recalled = seen["prompt"].split("whenever they happened:", 1)[1]
        assert recalled.strip() == "- verdict: c"

    def test_reading_from_nothing_leaves_the_prompt_alone(self, built):
        import runner

        graph, manager, doc = built
        doc["tasks"][1]["memory"] = {"read_from": []}
        memory = FakeMemory(hits=[(FakeMessage('{"outputs": {"summary": "s"}}'), 0.9)])
        action = self.main_action(next(a for a in manager.agents if a.name == "ReportAgent"))
        seen = record_prompt(action)
        runner._attach_ltm(manager, graph, doc, {"report": memory}, {})
        run_action(action, {"verdict": "c"})

        # Write-only: it still remembers, it just is not told about it again.
        assert memory.searches == []
        assert "Similar past runs" not in seen["prompt"]

    main_action = staticmethod(TestRecall.__dict__["main_action"].__func__)
    built = TestRecall.__dict__["built"]


class TestQuery:
    def test_it_searches_with_the_fields_the_node_stores(self):
        import memory_policy

        task = {"name": "judge", "description": "decide",
                "inputs": [{"name": "company"}, {"name": "news"}],
                "outputs": [{"name": "verdict"}],
                "memory": {"inputs": ["company"]}}
        query = memory_policy.query_for(task, {"company": "Gaucho", "news": "n" * 5000})

        # Searching with a page of news it never wrote down is noise against
        # every stored entry equally.
        assert "Gaucho" in query
        assert "n" * 100 not in query

    def test_a_node_that_stores_no_inputs_still_has_something_to_match_on(self):
        import memory_policy

        task = {"name": "judge", "description": "decide",
                "inputs": [{"name": "company"}], "outputs": [{"name": "verdict"}],
                "memory": {"inputs": []}}
        query = memory_policy.query_for(task, {"company": "Gaucho"})

        # The query is never written anywhere; it only has to rank entries.
        assert "Gaucho" in query
        assert "decide" in query


class TestTrackingOneSubject:
    """The two kinds are disjoint, and that is the point.

    A node used to get a similarity search *and*, if it named a subject, an
    exact-match block bolted alongside it — two retrieval paths with different
    semantics stapled together, and no way to say which held the truth. Coze
    keeps 数据库 and 知识库 apart and makes you pick; so does this now.
    """

    def test_a_node_that_names_a_subject_reads_its_table_only(
            self, built, tmp_path, monkeypatch):
        import memory_store
        import runner
        import table_store

        monkeypatch.setattr(table_store, "TABLES_DIR", tmp_path / "tables")
        graph, manager, doc = built
        doc["tasks"][1]["memory"] = {"match": "verdict", "retrieve": 3}
        table_store.upsert("g1", "report", "concern", "",
                           {"inputs": {"verdict": "concern"},
                            "outputs": {"summary": "last time"}}, "2026-01-01")

        # Both of the old sources are left armed: neither may be consulted.
        store = FakeMemory(hits=[(FakeMessage(json.dumps(
            {"inputs": {"verdict": "other"}, "outputs": {"summary": "a similar one"}}
        )), 0.9)])
        monkeypatch.setattr(memory_store, "list_entries", lambda gid, agent: [
            {"timestamp": "2026-01-01", "content": json.dumps(
                {"inputs": {"verdict": "concern"},
                 "outputs": {"summary": "from the corpus"}})}])

        action = self.main_action(
            next(a for a in manager.agents if a.name == "ReportAgent"))
        seen = record_prompt(action)
        runner._attach_ltm(manager, graph, doc, {"report": store}, {})
        run_action(action, {"verdict": "concern"})

        assert "last time" in seen["prompt"]            # the table
        assert "a similar one" not in seen["prompt"]    # not the vector search
        assert "from the corpus" not in seen["prompt"]  # not the old scan
        assert store.searches == [], "a table node searched a vector store"

    def test_a_node_with_no_subject_still_gets_similarity_search(self, built):
        import runner

        graph, manager, doc = built
        doc["tasks"][1]["memory"] = {"retrieve": 3}     # no `match`
        store = FakeMemory(hits=[(FakeMessage(json.dumps(
            {"inputs": {"verdict": "other"},
             "outputs": {"summary": "a similar one"}})), 0.9)])
        action = self.main_action(
            next(a for a in manager.agents if a.name == "ReportAgent"))
        seen = record_prompt(action)
        runner._attach_ltm(manager, graph, doc, {"report": store}, {})
        run_action(action, {"verdict": "concern"})

        assert "a similar one" in seen["prompt"]

    main_action = staticmethod(TestRecall.__dict__["main_action"].__func__)
    built = TestRecall.__dict__["built"]


class TestConcurrentRuns:
    """A batch runs its records at the same time, into one store per node."""

    def test_every_run_of_a_batch_keeps_its_entry(self, monkeypatch):
        import memory_store
        import runner

        # `save()` writes the whole corpus. A run that adds to the copy it
        # loaded when it started puts the store back as it was and loses every
        # entry written since — which in a batch of seven meant five of them.
        written = []

        class Store:
            def __init__(self):
                self.mine = []

            def add(self, messages):
                self.mine.extend(messages)

            def save(self):
                written.extend(self.mine)
                self.mine = []

        stores = {}
        monkeypatch.setattr(memory_store, "open_memory",
                            lambda gid, agent, create=False:
                            stores.setdefault(agent, Store()))

        stale = Store()          # what each run has been holding all along
        for company in ("a", "b", "c"):
            runner._save_ltm(
                graph_doc(), {"judge": stale},
                FakeGraph([FakeNode("judge", ["company"], ["verdict"])]),
                FakeWorkflow({"company": company, "verdict": "v"}), {},
            )

        assert len(written) == 3
        assert stale.mine == []   # nothing was written through the held copy


class TestValidationAtSaveTime:
    def test_the_graph_refuses_a_field_the_node_does_not_have(self):
        import graphs

        with pytest.raises(graphs.GraphValidationError) as excinfo:
            graphs.validate_task_memory(graph_doc(outputs=["verdcit"])["tasks"])
        assert "verdcit" in str(excinfo.value)

    def test_a_valid_selection_passes(self):
        import graphs

        graphs.validate_task_memory(graph_doc(outputs=["verdict"])["tasks"])

    def test_the_policy_never_reaches_the_framework(self):
        import graphs

        stripped = graphs.strip_task(graph_doc(outputs=["verdict"])["tasks"][0])
        # The framework rejects task keys it does not know.
        assert "memory" not in stripped
        assert "use_long_term_memory" not in stripped


class TestExportedProject:
    @staticmethod
    def build(doc):
        """The exported project's files, by their path inside the archive."""
        import io
        import zipfile

        import export_api

        _, payload = export_api.build_project(doc)
        archive = zipfile.ZipFile(io.BytesIO(payload))
        return {n.split("/", 1)[1]: archive.read(n).decode("utf-8")
                for n in archive.namelist()
                if "/" in n and not n.endswith("/") and not n.endswith(".pyc")}

    @pytest.fixture
    def project(self):
        return self.build(graph_doc(outputs=["verdict"], inputs=[]))

    def test_it_bundles_the_same_selection_rule(self, project):
        # Re-implementing it in the generated file would let the two drift.
        assert "vendor/memory_policy.py" in project
        assert "def select(" in project["vendor/memory_policy.py"]

    def test_the_generated_workflow_writes_memory(self, project):
        # It used to open a store, read from it, and never add anything, so a
        # deployed project's memory stayed empty for ever.
        source = project["workflow.py"]
        assert "def save_ltm(" in source
        assert "memory.add(" in source
        assert "memory_policy.select(" in source

    def test_the_selection_travels_with_the_tasks(self, project):
        assert '"outputs": [\'verdict\']' in project["workflow.py"] \
            or "'outputs': ['verdict']" in project["workflow.py"]

    def test_the_policy_is_kept_out_of_the_framework_tasks(self, project):
        assert 'k not in ("use_long_term_memory", "memory")' in project["workflow.py"]

    def test_it_searches_when_each_node_runs(self, project):
        source = project["workflow.py"]
        # Opening the stores no longer searches them: the search needs inputs
        # that do not exist until everything upstream has finished.
        assert "def prepare_ltm(tasks):" in source
        assert "def recall_before_running(" in source
        assert "memory_policy.recall_async(" in source

    def test_it_finds_the_agent_through_the_node(self, project):
        source = project["workflow.py"]
        # memories.get(agent.name) matched nothing and attached nothing.
        assert "def agent_for_node(" in source
        assert "memories.get(agent.name)" not in source

    def test_a_project_with_no_memory_does_not_bundle_the_module(self):
        doc = graph_doc()
        doc["tasks"][0]["use_long_term_memory"] = False
        assert "vendor/memory_policy.py" not in self.build(doc)


class TestTheRunnerHoldsRecallToTheRunsOwnDate:
    """The date a node is judged by has to survive the trip to recall.

    `as_of` is a context field: the node is dated by it but never takes it as
    an input, so the inputs the framework hands to recall do not contain it.
    Unless the runner passes the run's own data alongside, the cutoff is
    computed from a blank and a stepped backtest quietly reads its own future.
    """

    @pytest.fixture
    def wired(self, tmp_path, monkeypatch):
        import table_store
        from evoagentx.agents.agent_manager import AgentManager
        from evoagentx.models import LiteLLMConfig
        from evoagentx.workflow.workflow_graph import SequentialWorkFlowGraph

        monkeypatch.setattr(table_store, "TABLES_DIR", tmp_path / "tables")
        tasks = [{"name": "judge", "description": "decide",
                  "inputs": [{"name": "company", "type": "str",
                              "description": "c", "required": True}],
                  "outputs": [{"name": "verdict", "type": "str",
                               "description": "v", "required": True}],
                  "prompt": "Company: {company}", "parse_mode": "str"}]
        graph = SequentialWorkFlowGraph(goal="g", tasks=tasks)
        manager = AgentManager()
        manager.add_agents_from_workflow(
            graph, llm_config=LiteLLMConfig(model="deepseek/deepseek-chat",
                                            deepseek_key="test-only"))

        table_store.upsert("g1", "judge", "Lucid", "2026-04-16",
                           {"inputs": {"company": "Lucid"},
                            "outputs": {"verdict": "SUPPRESS FROM THE FUTURE"}},
                           "2026-09-07 01:09:39")
        table_store.upsert("g1", "judge", "Lucid", "2025-08-01",
                           {"inputs": {"company": "Lucid"},
                            "outputs": {"verdict": "an earlier look"}},
                           "2026-09-07 01:00:00")

        doc = {"id": "g1", "goal": "g", "edges": [], "tasks": [{
            "name": "judge", "use_long_term_memory": True,
            "inputs": [{"name": "company"}], "outputs": [{"name": "verdict"}],
            "memory": {"match": "company", "at": "as_of", "retrieve": 5,
                       "context": ["as_of"], "inputs": ["company"]},
        }]}
        return graph, manager, doc

    def prompt_for(self, wired, state):
        import runner

        graph, manager, doc = wired
        # No vector store at all: a table node must not need one.
        runner._attach_ltm(manager, graph, doc, {}, state)
        action = TestRecall.main_action(
            next(a for a in manager.agents if a.name == "JudgeAgent"))
        seen = record_prompt(action)
        runner._attach_ltm(manager, graph, doc, {}, state)
        run_action(action, {"company": "Lucid"})
        return seen["prompt"]

    def test_a_later_row_does_not_reach_a_dated_run(self, wired):
        # The run is dated 2025-11-17; the table already holds 2026-04-16,
        # because the two batches overlapped in wall-clock time.
        prompt = self.prompt_for(
            wired, {"_effective_inputs": {"company": "Lucid", "as_of": "2025-11-17"}})

        assert "SUPPRESS FROM THE FUTURE" not in prompt
        assert "an earlier look" in prompt

    def test_the_run_with_no_date_reads_the_whole_record(self, wired):
        # A workflow that never dated its memory keeps working exactly as it
        # did, rather than silently recalling nothing.
        prompt = self.prompt_for(wired, {"_effective_inputs": {"company": "Lucid"}})

        assert "SUPPRESS FROM THE FUTURE" in prompt
        assert "an earlier look" in prompt

    def test_a_table_node_needs_no_vector_store(self, wired):
        # It used to load FAISS and an embedding model per node to build an
        # index nothing read.
        prompt = self.prompt_for(
            wired, {"_effective_inputs": {"company": "Lucid", "as_of": "2026-01-01"}})
        assert "an earlier look" in prompt


class TestTheExportedProjectDatesItsMemoryToo:
    """An exported project has its own copy of the wiring.

    It shipped without either half: `save_ltm` never passed the run's data, so
    entries carried no `as_of` at all, and recall never passed it either, so
    there was no date to filter on. A project exported from a workflow whose
    memory is dated would quietly behave as though it were not.
    """

    @pytest.fixture
    def memory_graph(self):
        return {
            "id": "g1", "name": "G", "goal": "g", "edges": [],
            "tasks": [{
                "name": "judge", "description": "d", "prompt": "C: {company}",
                "parse_mode": "str", "use_long_term_memory": True,
                "inputs": [{"name": "company", "type": "str",
                            "description": "c", "required": True}],
                "outputs": [{"name": "verdict", "type": "str",
                             "description": "v", "required": True}],
                "memory": {"match": "company", "at": "as_of",
                           "context": ["as_of"], "inputs": ["company"]},
            }],
        }

    @pytest.fixture
    def generated(self, memory_graph):
        import export_api

        files, _ = export_api.project_files(memory_graph, include_vendor=False)
        return files["workflow.py"]

    def test_it_is_valid_python(self, generated):
        import ast
        ast.parse(generated)

    def test_what_it_writes_records_the_date(self, generated):
        assert "run_data=data," in generated

    def test_what_it_reads_is_held_to_the_date(self, generated):
        assert "run_data=run_data," in generated
        assert "attach_ltm(manager, graph, tasks, memories, run_data=inputs)" in generated

    def test_it_carries_the_policy_rather_than_a_second_copy_of_it(self, memory_graph):
        # The cutoff lives in memory_policy; an export that reimplemented it
        # would be a place for the two to drift apart.
        import export_api
        files, _ = export_api.project_files(memory_graph, include_vendor=True)

        assert "vendor/memory_policy.py" in files
        assert "_earlier" in files["vendor/memory_policy.py"]


class TestTheStoreHoldsOneEntryPerSubject:
    """End to end against a real store, because the shape is only worth
    anything if the write path produces it.

    A batch is several runs of the same node writing concurrently, and each
    one is a read-modify-write of the subject's entry. Getting that wrong
    silently costs history.
    """

    task = {
        "name": "judge",
        "description": "d",
        "memory": {"match": "company", "at": "as_of",
                   "context": ["as_of"], "inputs": ["company"]},
    }

    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        import memory_store
        monkeypatch.setattr(memory_store, "MEMORY_DIR", tmp_path / "memory")
        return memory_store

    def write(self, store, company, as_of, verdict):
        import runner
        from evoagentx.core.message import Message, MessageType

        payload = {"task": "judge",
                   "inputs": {"company": company, "as_of": as_of},
                   "outputs": {"verdict": verdict}}

        def as_message(body):
            return Message(content=json.dumps(body, ensure_ascii=False, default=str),
                           msg_type=MessageType.RESPONSE, agent="judge",
                           wf_goal="g", wf_task="judge", wf_task_desc="d")

        with runner._ltm_lock("g1/judge"):
            memory = store.open_memory("g1", "judge", create=True)
            runner._write_entry(memory, "g1", "judge", self.task, payload, as_message)
            memory.save()

    def entries(self, store):
        return store.list_entries("g1", "judge")

    def test_six_runs_of_one_company_are_one_entry(self, store):
        for month in range(1, 7):
            self.write(store, "Sleep Number", f"2026-{month:02d}-01", f"v{month}")

        kept = self.entries(store)
        assert len(kept) == 1, f"expected one entry per company, got {len(kept)}"

        body = json.loads(kept[0]["content"])
        assert body["subject"] == {"company": "Sleep Number"}
        assert [p["at"] for p in body["timeline"]] == [
            f"2026-{m:02d}-01" for m in range(1, 7)]

    def test_two_companies_are_two_entries(self, store):
        self.write(store, "Sleep Number", "2026-01-01", "a")
        self.write(store, "Wayfair", "2026-01-01", "b")

        subjects = {json.loads(e["content"])["subject"]["company"]
                    for e in self.entries(store)}
        assert subjects == {"Sleep Number", "Wayfair"}

    def test_concurrent_writers_all_survive(self, store):
        # Records of a batch run in parallel; each rewrites the same entry.
        import threading

        dates = [f"2026-{m:02d}-01" for m in range(1, 9)]
        threads = [threading.Thread(target=self.write,
                                    args=(store, "Sleep Number", d, f"v{d}"))
                   for d in dates]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        seen = {p["at"] for e in self.entries(store)
                for p in json.loads(e["content"])["timeline"]}
        assert seen == set(dates), f"lost {sorted(set(dates) - seen)}"

    def test_recall_reads_the_history_back(self, store):
        import memory_policy

        for month in (1, 3, 6):
            self.write(store, "Sleep Number", f"2026-{month:02d}-01", f"v{month}")

        block = memory_policy.history_for(
            self.entries(store), self.task,
            {"company": "Sleep Number", "as_of": "2026-05-01"})

        assert "v1" in block and "v3" in block
        assert "v6" not in block          # still after the run being processed


class TestTheExportedProjectKeepsTablesToo:
    """The export has its own copy of the wiring, and shipped without it
    twice already: no `as_of` recorded, then no date to filter on."""

    @pytest.fixture
    def generated(self):
        import export_api

        graph = {
            "id": "g1", "name": "G", "goal": "g", "edges": [],
            "tasks": [{
                "name": "judge", "description": "d", "prompt": "C: {company}",
                "parse_mode": "str", "use_long_term_memory": True,
                "inputs": [{"name": "company", "type": "str",
                            "description": "c", "required": True}],
                "outputs": [{"name": "verdict", "type": "str",
                             "description": "v", "required": True}],
                "memory": {"match": "company", "at": "as_of",
                           "context": ["as_of"], "inputs": ["company"]},
            }],
        }
        files, _ = export_api.project_files(graph)
        return files

    def test_it_is_valid_python(self, generated):
        import ast
        ast.parse(generated["workflow.py"])

    def test_the_table_travels_with_it(self, generated):
        # Not re-implemented in the generated file, where it could drift.
        assert "vendor/table_store.py" in generated
        assert "recorded_at" in generated["vendor/table_store.py"]

    def test_it_writes_rows(self, generated):
        assert "_table_store().upsert(" in generated["workflow.py"]

    def test_it_reads_them_point_in_time(self, generated):
        assert "table=_table_store()" in generated["workflow.py"]

    def test_the_table_lands_in_the_project_not_the_studio_checkout(self, generated):
        # The vendored module derives its path from the repo it was copied
        # from; left alone an exported project would write into Studio's data.
        assert 'TABLES_DIR = HERE / "memory_tables"' in generated["workflow.py"]

    def test_a_table_node_opens_no_corpus(self, generated):
        assert 'if memory_policy.policy(task)["kind"] == "table":' \
            in generated["workflow.py"]
