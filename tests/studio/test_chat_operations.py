"""Tests for the chat operation engine (backend/api/chat_api.py).

The engine applies a model's proposed edits to a canvas graph. It is the one
place where an LLM's output mutates the user's workflow, so the behaviours that
matter are the defensive ones: a malformed operation must not discard the good
ones, a rename must not orphan edges, and the caller's graph must come back
untouched when something goes wrong.

These cover the bugs found while building the feature:

1. `applied` was true whenever any operation ran, so a turn of pure reads
   created a bogus undo entry on the client.
2. Nodes added with a name already on the canvas silently collided.
3. `generate_workflow` mixed with other operations replaced the canvas and
   discarded them without saying so.
"""

from conftest import make_graph, make_task


def apply(graph, operations, graph_id=None):
    from backend.api import chat_api
    return chat_api.apply_operations(graph, operations, graph_id)


class TestNodeEditing:
    def test_add_node_slugifies_and_deduplicates(self):
        graph = make_graph([make_task("a", outputs=["x"])])
        result = apply(graph, [
            {"op": "add_node", "task": make_task("C Node!", outputs=["z"])},
            {"op": "add_node", "task": make_task("a", outputs=["y"])},
        ])
        names = [t["name"] for t in result["graph"]["tasks"]]
        assert names == ["a", "c_node", "a_2"]
        assert any("already taken" in note for note in result["notes"])

    def test_update_node_merges_and_keeps_other_fields(self):
        graph = make_graph([make_task("a", inputs=["q"], outputs=["x"])])
        result = apply(graph, [
            {"op": "update_node", "name": "a", "patch": {"parse_mode": "json"}},
        ])
        task = result["graph"]["tasks"][0]
        assert task["parse_mode"] == "json"
        assert [i["name"] for i in task["inputs"]] == ["q"]

    def test_update_node_cannot_rename(self):
        graph = make_graph([make_task("a", outputs=["x"])])
        result = apply(graph, [
            {"op": "update_node", "name": "a", "patch": {"name": "b", "parse_mode": "json"}},
        ])
        assert result["graph"]["tasks"][0]["name"] == "a"
        assert result["graph"]["tasks"][0]["parse_mode"] == "json"

    def test_rename_rewires_edges(self):
        graph = make_graph(
            [make_task("a", outputs=["x"]), make_task("b", inputs=["x"], outputs=["y"])],
            edges=[("a", "b")],
        )
        result = apply(graph, [{"op": "rename_node", "name": "b", "new_name": "middle"}])
        assert [t["name"] for t in result["graph"]["tasks"]] == ["a", "middle"]
        assert result["graph"]["edges"] == [{"source": "a", "target": "middle"}]

    def test_delete_node_removes_its_edges(self):
        graph = make_graph(
            [make_task("a", outputs=["x"]), make_task("b", inputs=["x"], outputs=["y"])],
            edges=[("a", "b")],
        )
        result = apply(graph, [{"op": "delete_node", "name": "b"}])
        assert [t["name"] for t in result["graph"]["tasks"]] == ["a"]
        assert result["graph"]["edges"] == []


class TestFailureIsolation:
    def test_one_bad_operation_does_not_lose_the_others(self):
        graph = make_graph([make_task("a", outputs=["x"])])
        result = apply(graph, [
            {"op": "add_node", "task": make_task("b", inputs=["x"], outputs=["y"])},
            {"op": "add_edge", "source": "ghost", "target": "a"},
            {"op": "frobnicate"},
            {"op": "add_edge", "source": "a", "target": "b"},
        ])
        assert [t["name"] for t in result["graph"]["tasks"]] == ["a", "b"]
        assert result["graph"]["edges"] == [{"source": "a", "target": "b", "mappings": [{"from": "x", "to": "x"}]}]
        assert len(result["notes"]) == 2

    def test_unknown_operation_is_reported_not_raised(self):
        result = apply(make_graph([]), [{"op": "nope"}])
        assert any("nope" in note for note in result["notes"])

    def test_non_object_operation_is_skipped(self):
        result = apply(make_graph([]), ["not an operation"])
        assert result["notes"]

    def test_caller_graph_is_never_mutated(self):
        graph = make_graph([make_task("a", outputs=["x"])], edges=[])
        before = repr(graph)
        apply(graph, [
            {"op": "add_node", "task": make_task("b", outputs=["y"])},
            {"op": "delete_node", "name": "a"},
        ])
        assert repr(graph) == before

    def test_self_edge_is_refused(self):
        graph = make_graph([make_task("a", outputs=["x"])])
        result = apply(graph, [{"op": "add_edge", "source": "a", "target": "a"}])
        assert result["graph"]["edges"] == []
        assert any("itself" in note for note in result["notes"])

    def test_duplicate_edge_is_not_added_twice(self):
        graph = make_graph(
            [make_task("a", outputs=["x"]), make_task("b", inputs=["x"], outputs=["y"])],
            edges=[("a", "b")],
        )
        result = apply(graph, [{"op": "add_edge", "source": "a", "target": "b"}])
        assert result["graph"]["edges"] == [{"source": "a", "target": "b"}]


class TestSideEffectingOperations:
    def test_run_workflow_only_proposes(self, studio_data):
        """A run costs money and its tools touch the world: the user confirms."""
        from backend.api import graphs as graph_store
        created = graph_store.create_graph("Runnable", "goal")
        graph = make_graph([make_task("a", inputs=["topic"], outputs=["x"])],
                           id=created["id"])
        result = apply(graph, [{"op": "run_workflow", "inputs": {"topic": "t"}}],
                       created["id"])
        assert result["pending_run"]["inputs"] == {"topic": "t"}
        assert result["pending_run"]["plan_id"]
        assert result["pending_run"]["input_errors"] == []
        assert result["saved"] is False

    def test_save_graph_refuses_an_invalid_workflow(self, studio_data):
        from backend.api import graphs as graph_store
        created = graph_store.create_graph("Broken", "goal")
        # Referencing a skill that does not exist fails validation.
        task = make_task("a", inputs=["topic"], outputs=["x"], skill_names=["ghost"])
        result = apply(make_graph([task], id=created["id"]),
                       [{"op": "save_graph"}], created["id"])
        assert result["saved"] is False
        assert any("save_graph refused" in note for note in result["notes"])

    def test_create_skill_then_delete(self, studio_data):
        from backend.api import skills_api
        result = apply(make_graph([]), [
            {"op": "create_skill", "spec": {"name": "tone", "description": "d",
                                            "content": "# Tone"}},
        ])
        assert result["skills_created"] == ["tone"]
        assert skills_api.get_skill("tone")["content"] == "# Tone"

        result = apply(make_graph([]), [{"op": "delete_skill", "name": "tone"}])
        assert skills_api.get_skill("tone") is None

    def test_delete_missing_skill_is_a_note(self, studio_data):
        result = apply(make_graph([]), [{"op": "delete_skill", "name": "ghost"}])
        assert any("ghost" in note for note in result["notes"])


class TestReadOperations:
    def test_validate_reports_errors_without_changing_the_graph(self, studio_data):
        task = make_task("a", inputs=["topic"], outputs=["x"], skill_names=["ghost"])
        graph = make_graph([task])
        result = apply(graph, [{"op": "validate"}])
        observation = result["observations"][0]
        assert observation["op"] == "validate"
        assert observation["result"]["errors"]
        assert result["graph"]["tasks"] == graph["tasks"]

    def test_input_missing_from_the_prompt_still_validates(self, studio_data):
        """A known gap, pinned here so a future fix updates this test.

        The framework only rejects an input the prompt never references when
        the agent is built, i.e. during a run — after the user has paid for it.
        Save-time validation lets it through, which is why the Inspector warns
        about it in the editor instead.
        """
        task = make_task("a", inputs=["topic"], outputs=["x"], prompt="no placeholder")
        result = apply(make_graph([task]), [{"op": "validate"}])
        assert result["observations"][0]["result"]["errors"] == []

    def test_validate_lists_workflow_inputs_when_sound(self, studio_data):
        graph = make_graph([make_task("a", inputs=["topic"], outputs=["x"])])
        result = apply(graph, [{"op": "validate"}])
        found = result["observations"][0]["result"]
        assert found["errors"] == []
        assert [i["name"] for i in found["workflow_inputs"]] == ["topic"]

    def test_inspect_node_returns_the_stored_task(self):
        graph = make_graph([make_task("a", outputs=["x"])])
        result = apply(graph, [{"op": "inspect_node", "name": "a"}])
        assert result["observations"][0]["result"]["name"] == "a"

    def test_inspect_missing_node_is_a_note(self):
        result = apply(make_graph([]), [{"op": "inspect_node", "name": "ghost"}])
        assert result["observations"][0]["op"] == "operation_error"
        assert any("ghost" in note for note in result["notes"])


class TestGeneratedFlow:
    def test_generation_normalizes_bare_edges_even_for_current_version_canvas(self, monkeypatch):
        import copy, time
        from backend.api import chat_api, graphs
        tasks = [make_task('read', inputs=['file'], outputs=['rows']),
                 make_task('clean', inputs=['rows'], outputs=['cleaned']),
                 make_task('report', inputs=['cleaned'], outputs=['report'])]
        generated = {'tasks': tasks, 'edges': [{'source': 'read', 'target': 'clean'}, {'source': 'clean', 'target': 'report'}, {'source': 'read', 'target': 'report'}]}
        monkeypatch.setattr(chat_api, '_workflow_from_goal', lambda *a, **kw: copy.deepcopy(generated))
        original = {'id': 'generation-test', 'flow_version': graphs.FLOW_VERSION, 'tasks': [], 'edges': []}
        id = chat_api.start_generation(original, 'Read clean and report file data', 'generation-test')
        for _ in range(100):
            job = chat_api.generation_job('generation-test', id)
            if job['status'] != 'running': break
            time.sleep(.01)
        assert job['status'] == 'done', job.get('error')
        graph = job['graph']
        assert graph['edges'][0]['mappings'] == [{'from': 'rows', 'to': 'rows'}]
        assert graph['edges'][1]['mappings'] == [{'from': 'cleaned', 'to': 'cleaned'}]
        assert graph['edges'][2]['control_only'] is True
        bindings = graphs.compile_bindings(graph['tasks'], graph['edges'])
        assert 'rows' in bindings['clean'] and 'cleaned' in bindings['report']
        assert original['tasks'] == [] and 'mappings' not in generated['edges'][0]
        import pytest
        from fastapi import HTTPException
        with pytest.raises(HTTPException): chat_api.generation_job('another-task', id)

    def test_invalid_explicit_mapping_is_not_replaced_to_force_success(self, monkeypatch):
        import time
        from backend.api import chat_api
        monkeypatch.setattr(chat_api, '_workflow_from_goal', lambda *a, **kw: {
            'tasks': [make_task('read', outputs=['rows']), make_task('clean', inputs=['rows'], outputs=['result'])],
            'edges': [{'source': 'read', 'target': 'clean', 'mappings': [{'from': 'missing', 'to': 'rows'}]}]})
        original = {'id': 'bad-generation', 'tasks': [], 'edges': []}
        id = chat_api.start_generation(original, 'Generate an invalid mapping example', 'bad-generation')
        for _ in range(100):
            job = chat_api.generation_job('bad-generation', id)
            if job['status'] != 'running': break
            time.sleep(.01)
        assert job['status'] == 'failed'
        assert job['graph'] is None and job['draft_graph']['tasks']
        assert original['tasks'] == []


def test_chat_continues_with_add_update_and_delete_on_current_canvas(monkeypatch):
    import json
    from backend.api import chat_api, graphs
    monkeypatch.setattr(graphs, 'graph_exists', lambda _: True)
    graph = {'id': 'conversation', 'flow_version': graphs.FLOW_VERSION, 'tasks': [make_task('read', inputs=['file'], outputs=['rows'])], 'edges': []}
    commands = [
        [{'op': 'add_node', 'task': make_task('clean', inputs=['rows'], outputs=['cleaned'])}, {'op': 'add_edge', 'source': 'read', 'target': 'clean'}],
        [{'op': 'update_node', 'name': 'clean', 'patch': {'description': 'Normalize and remove duplicates'}}],
        [{'op': 'delete_node', 'name': 'clean'}],
    ]
    calls = []
    def ask(messages):
        calls.append(messages)
        return json.dumps({'reply': 'Applied requested edit', 'operations': commands.pop(0)})
    monkeypatch.setattr(chat_api, '_ask', ask)
    history = []
    for prompt in ['Add a cleaning step', 'Also remove duplicates there', 'Delete that step']:
        result = chat_api.chat('conversation', {'graph': graph, 'message': prompt, 'history': history})
        assert result['applied'] and result['errors'] == [], result
        graph = result['graph']
        graphs.validate_graph(graph)
        history += [{'role': 'user', 'content': prompt}, {'role': 'assistant', 'content': result['reply']}]
    assert [t['name'] for t in graph['tasks']] == ['read'] and graph['edges'] == []
    assert any(m.get('content') == 'Add a cleaning step' for m in calls[-1])
    assert 'Normalize and remove duplicates' in calls[-1][0]['content']


def test_chat_explicit_mapping_can_update_an_existing_connection():
    from backend.api import chat_api
    graph = make_graph([make_task('a', outputs=['x', 'y']), make_task('b', inputs=['z'], outputs=['result'])], edges=[('a', 'b')])
    result = apply(graph, [{'op': 'add_edge', 'source': 'a', 'target': 'b', 'mappings': [{'from': 'y', 'to': 'z'}]}])
    assert result['graph']['edges'] == [{'source': 'a', 'target': 'b', 'mappings': [{'from': 'y', 'to': 'z'}]}]
    assert chat_api._validate(result['graph']) == []


def test_chat_save_returns_new_identity_and_can_continue_after_rename(studio_data):
    from backend.api import graphs, chat_api
    graph = graphs.create_graph('Before chat rename', 'Goal')
    graph = graphs.save_graph(graph['id'], {**graph, 'flow_version': graphs.FLOW_VERSION, 'tasks': [make_task('read', inputs=['input'], outputs=['rows'])], 'edges': []})
    old_id = graph['id']
    result = apply(graph, [{'op': 'set_name', 'name': 'After chat rename'}, {'op': 'save_graph'}, {'op': 'save_graph'}], old_id)
    assert result['saved']
    assert result['graph']['id'] != old_id
    assert graphs.graph_exists(result['graph']['id']) and not graphs.graph_exists(old_id)
    assert result['graph']['flow_version'] == graphs.FLOW_VERSION


def test_saving_current_flow_does_not_restore_deleted_connection(studio_data):
    from backend.api import graphs
    graph = graphs.create_graph('Disconnected workflow', 'Goal')
    graph = graphs.save_graph(graph['id'], {**graph, 'flow_version': graphs.FLOW_VERSION,
        'tasks': [make_task('extract', outputs=['signals']), make_task('flag', inputs=['signals'], outputs=['alerts'])],
        'edges': [{'source': 'extract', 'target': 'flag', 'mappings': [{'from': 'signals', 'to': 'signals'}]}]})
    graph['edges'] = []
    saved = graphs.save_graph(graph['id'], graph)
    assert saved['edges'] == []
    assert graphs.load_graph(graph['id'])['edges'] == []


def test_chat_run_plan_and_partial_execution_use_current_settings():
    from backend.api import graphs
    graph = make_graph([make_task('read', inputs=['file'], outputs=['rows']), make_task('clean', inputs=['rows'], outputs=['cleaned'])], edges=[])
    graph.update(id='g', flow_version=graphs.FLOW_VERSION)
    graph['edges'] = [{'source': 'read', 'target': 'clean', 'mappings': [{'from': 'rows', 'to': 'rows'}]}]
    planned = apply(graph, [{'op': 'plan_workflow', 'start_at': ['clean'], 'inputs': {}}], 'g')
    plan = planned['observations'][0]['result']
    assert plan['start_at'] == ['clean']
    assert plan['input_errors'] and plan['inputs'][0]['name'] == 'rows'
    proposed = apply(graph, [{'op': 'run_workflow', 'start_at': ['clean'], 'inputs': {'rows': 'some data'}}], 'g')['pending_run']
    assert proposed['plan_id'] == plan['plan_id'] and not proposed['input_errors']


def test_chat_cannot_read_or_stop_another_workflows_run(monkeypatch):
    from backend.api import runner
    monkeypatch.setattr(runner, 'get_run', lambda id: {'run_id': id, 'graph_id': 'foreign', 'status': 'running'})
    cancelled = []
    monkeypatch.setattr(runner, 'cancel_run', lambda id: cancelled.append(id))
    for op in ['read_run', 'cancel_run']:
        result = apply(make_graph([]), [{'op': op, 'run_id': 'foreign-run'}], 'current')
        assert result['observations'][0]['op'] == 'operation_error'
    assert cancelled == []


def test_chat_workflow_configuration_and_panel_are_explicit():
    result = apply(make_graph([]), [{'op': 'configure_workflow', 'patch': {'output_dir': 'reports'}}, {'op': 'open_panel', 'panel': 'runs'}], 'g')
    assert result['graph']['output_dir'] == 'reports' and result['panel'] == 'runs'
    rejected = apply(make_graph([]), [{'op': 'configure_workflow', 'patch': {'id': 'other'}}], 'g')
    assert rejected['observations'][0]['op'] == 'operation_error'


def test_schedule_configuration_validates_and_uses_saved_identity(studio_data, monkeypatch):
    from backend.api import graphs, scheduler
    graph = graphs.create_graph('Scheduled test', 'Goal')
    graph = graphs.save_graph(graph['id'], {**graph, 'tasks': [make_task('work', inputs=['topic'], outputs=['result'])], 'edges': []})
    called = []
    monkeypatch.setattr(scheduler, 'set_schedule', lambda graph, config: called.append((graph['id'], config)) or {'scheduled': True})
    missing = apply(graph, [{'op': 'set_schedule', 'schedule': {'mode': 'interval', 'interval_minutes': 60}}], graph['id'])
    assert not called and missing['observations'][0]['op'] == 'operation_error'
    valid = apply(graph, [{'op': 'set_schedule', 'schedule': {'mode': 'interval', 'interval_minutes': 60, 'inputs': {'topic': 'example'}}}], graph['id'])
    assert valid['saved'] and called[0][1]['inputs'] == {'topic': 'example'}
