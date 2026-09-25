"""A workflow's evaluators and the Evolve that uses them: what must not leak,
what must not be lost, and what a draft may not block."""
import copy
import json

import pytest
from fastapi.testclient import TestClient

from backend.api import graphs, runner, sources, tools_registry
from backend.features.data import dataloaders
from backend.features.evaluation import canvas_evolution, evaluator_tools
from backend.features.evaluation import saved_result_evolution as saved

SECRET = 'answer-that-must-stay-hidden'


MATCH = '''def evaluate(records, label_field: str = "expected"):
    """Score each run's answer against the label that came with its inputs."""
    scores = []
    for record in records:
        expected = (record.get("inputs") or {}).get(label_field)
        produced = ((record.get("node_outputs") or {}).get("work") or {}).get("answer")
        scores.append(None if record.get("status") != "success" or expected is None
                      else float(produced == expected))
    scored = [s for s in scores if s is not None]
    return {"metrics": {"score": sum(scored) / len(scored) if scored else None},
            "records": [{"id": r["id"], "score": s} for r, s in zip(records, scores)],
            "coverage": {"unit": "records", "total": len(scores), "scored": len(scored),
                         "unscored": len(scores) - len(scored)}}
'''


def evaluator(code=None, **extra):
    return {'name': 'quality', 'code': MATCH if code is None else code, 'metric': 'score',
            'config': {'label_field': 'expected'}, 'timing': 'run', **extra}


def graph(evaluator_entry, prompt='old'):
    return {'id': 'audit', 'name': 'Audit', 'goal': 'g', 'flow_version': 3,
            'tasks': [{'name': 'work', 'prompt': prompt, 'inputs': [{'name': 'text'}], 'outputs': [{'name': 'answer'}]}],
            'edges': [], 'evaluators': [evaluator_entry]}


@pytest.fixture(autouse=True)
def _evaluator_worker_in_process(monkeypatch):
    from backend.features.chat import chat_control
    from backend.features.evaluation.python_evaluator import execute_with_logs
    real = chat_control.worker
    monkeypatch.setattr(chat_control, 'worker',
                        lambda kind, payload, **kw: execute_with_logs(payload)
                        if kind == 'evaluate_python' else real(kind, payload, **kw))


class FakeLLM:
    def __init__(self):
        self.prompts = []

    def generate(self, prompt=None, **kw):
        self.prompts.append(prompt)
        return type('R', (), {'content': json.dumps({'prompts': {'work': 'new'}})})()


def fake_runs(monkeypatch):
    runs = {}

    def start(candidate, row, **kw):
        answer = 'yes' if candidate['tasks'][0]['prompt'] == 'new' else 'no'
        runs[kw['run_id']] = {'status': 'success', 'inputs': row, 'result': {'answer': answer},
                              'node_outputs': {'work': {'answer': answer}}, 'nodes': [],
                              'evaluated_in_run': [e['name'] for e in (candidate.get('evaluators') or [])
                                                   if e.get('enabled', True)]}
        return kw['run_id']
    monkeypatch.setattr(runner, 'start_run', start)
    monkeypatch.setattr(runner, 'get_run', lambda id: runs.get(id))
    return runs


class TestProposalsNeverSeeLabels:
    def test_frozen_label_records_do_not_reach_the_proposal_prompt(self, monkeypatch):
        fake_runs(monkeypatch)
        monkeypatch.setattr(dataloaders, 'records', lambda cfg: [{'id': '0', 'expected': SECRET}])

        code = '''def evaluate(records, label_records=None):
    ok = [r["prediction"] == {"answer": "yes"} for r in records]
    return {"metrics": {"score": sum(ok) / len(ok)},
            "records": [{"id": r["id"], "score": float(o),
                         "expected": label_records[0]["expected"]} for r, o in zip(records, ok)]}
'''
        llm = FakeLLM()
        monkeypatch.setattr(runner, '_make_llm', lambda **kw: llm)
        g = graph(evaluator(code, timing='batch', labels='r', config={}))
        state = {'task_id': 't1', 'execution_graph': copy.deepcopy(g)}
        canvas_evolution.execute(state, g, [{'text': 'q', 'id': '0'}],
                                 {'evaluator': 'quality', 'mode': 'evolve_evaluate', 'nodes': ['work'], 'rounds': 1}, lambda s: None)
        assert llm.prompts and SECRET not in llm.prompts[0]
        # Nor are the frozen labels kept (and persisted) with every report.
        assert SECRET not in json.dumps(state['baseline']['evaluations']['quality'].get('config'))
        assert state['optimized']['metrics']['score'] == 1

    def test_a_label_read_from_the_inputs_is_hidden_from_the_proposer(self, monkeypatch):
        fake_runs(monkeypatch)
        llm = FakeLLM()
        monkeypatch.setattr(runner, '_make_llm', lambda **kw: llm)
        g = graph(evaluator())
        state = {'task_id': 't2', 'execution_graph': copy.deepcopy(g)}
        canvas_evolution.execute(state, g, [{'text': 'q', 'expected': SECRET}],
                                 {'evaluator': 'quality', 'mode': 'evolve_evaluate', 'nodes': ['work'], 'rounds': 1}, lambda s: None)
        assert SECRET not in llm.prompts[0]
        assert '"text": "q"' in llm.prompts[0]

    def test_saved_result_labels_are_withheld_unless_allowed(self):
        records = [{'id': '0', 'run_id': 'r', 'inputs': {'text': 'q', 'gold': SECRET}, 'status': 'success',
                    'label': SECRET, 'prediction': 'no', 'nodes': []}]
        feedback = {'metrics': {'score': 0}, 'records': {'0': {**records[0], 'metrics': {'score': 0}}}}
        llm = FakeLLM()
        saved.propose(graph(evaluator()), records, ['work'], llm, feedback=feedback, hidden_inputs=['gold'])
        assert SECRET not in llm.prompts[0]
        assert '"score": 0' in llm.prompts[0]
        allowed = FakeLLM()
        saved.propose(graph(evaluator()), records, ['work'], allowed, share_labels=True)
        assert SECRET in allowed.prompts[0]


class TestCandidateReplay:
    def test_candidate_runs_do_not_execute_evaluators_twice(self, monkeypatch):
        runs = fake_runs(monkeypatch)
        monkeypatch.setattr(runner, '_make_llm', lambda **kw: FakeLLM())
        g = graph(evaluator())
        state = {'task_id': 't3', 'execution_graph': copy.deepcopy(g)}
        canvas_evolution.execute(state, g, [{'text': 'q', 'expected': 'yes'}],
                                 {'evaluator': 'quality', 'mode': 'evaluate', 'nodes': [], 'rounds': 1}, lambda s: None)
        assert all(r['evaluated_in_run'] == [] for r in runs.values())
        assert state['baseline']['metrics']['score'] == 0

    def test_candidates_are_recorded_with_their_prompts(self, monkeypatch):
        fake_runs(monkeypatch)
        monkeypatch.setattr(runner, '_make_llm', lambda **kw: FakeLLM())
        g = graph(evaluator())
        state = {'task_id': 't4', 'execution_graph': copy.deepcopy(g)}
        canvas_evolution.execute(state, g, [{'text': 'q', 'expected': 'yes'}],
                                 {'evaluator': 'quality', 'mode': 'evolve_evaluate', 'nodes': ['work'], 'rounds': 1}, lambda s: None)
        [candidate] = state['candidates']
        assert candidate['accepted'] and candidate['metrics']['score'] == 1
        assert candidate['changed'] == ['work']


class TestDrafts:
    def test_a_disabled_or_codeless_evaluator_does_not_block_saving(self):
        g = graph(evaluator(code='', timing='batch'))
        evaluator_tools.validate_graph(g)            # written, not typed into yet
        g['evaluators'][0].update(code='def broken(:', enabled=False)
        evaluator_tools.validate_graph(g)
        g['evaluators'][0]['enabled'] = True
        with pytest.raises(sources.SourceError, match="quality"):
            evaluator_tools.validate_graph(g)

    def test_two_evaluators_may_not_share_a_name(self):
        g = graph(evaluator())
        g['evaluators'].append(evaluator())
        with pytest.raises(sources.SourceError, match='unique'):
            evaluator_tools.validate_graph(g)
        g['evaluators'] = [evaluator(), {**evaluator(), 'name': ''}]
        with pytest.raises(sources.SourceError, match='needs a name'):
            evaluator_tools.validate_graph(g)

    def test_a_codeless_evaluator_reports_itself_and_cannot_be_an_objective(self):
        g = graph(evaluator(code=''))
        report = evaluator_tools.evaluate_runs(g, [{'status': 'success'}])['quality']
        assert report['status'] == 'failed' and 'code' in report['error']
        with pytest.raises(sources.SourceError, match='code'):
            canvas_evolution.select(g, 'quality')


class TestApply:
    def test_replace_moves_only_the_prompts_evolve_changed(self, tmp_path, monkeypatch):
        from backend.api import app as app_module, evolve_api
        monkeypatch.setattr(graphs, 'GRAPHS_DIR', tmp_path / 'graphs')
        monkeypatch.setattr(evolve_api, 'EVOLVE_DIR', tmp_path / 'evolve')
        monkeypatch.setattr(evolve_api, '_tasks', {})
        created = graphs.create_graph(name='Apply', goal='g')
        current = {**created, 'tasks': [{'name': 'work', 'prompt': 'old {text}', 'inputs': [{'name': 'text', 'type': 'str'}], 'outputs': [{'name': 'answer', 'type': 'str'}]},
                                        {'name': 'other', 'prompt': 'edited since {answer}', 'inputs': [{'name': 'answer', 'type': 'str'}], 'outputs': [{'name': 'final', 'type': 'str'}]}],
                   'edges': [{'source': 'work', 'target': 'other', 'mappings': [{'from': 'answer', 'to': 'answer'}]}]}
        graphs.save_graph(created['id'], current)
        snapshot = copy.deepcopy(current)
        snapshot['tasks'][0]['prompt'] = 'new {text}'
        snapshot['tasks'][1]['prompt'] = 'as it was when evolve started {answer}'
        evolve_api._tasks['x'] = {'task_id': 'x', 'graph_id': created['id'], 'status': 'done', 'optimized_graph': snapshot,
                                  'diff': [{'name': 'work', 'before': 'old {text}', 'after': 'new {text}', 'changed': True}]}
        client = TestClient(app_module.app)
        result = client.post('/api/evolve/x/apply?mode=replace')
        assert result.status_code == 200, result.text
        prompts = {t['name']: t['prompt'] for t in graphs.load_graph(created['id'])['tasks']}
        assert prompts == {'work': 'new {text}', 'other': 'edited since {answer}'}


def test_a_real_candidate_replay_runs_the_workflow_and_scores_it_once(monkeypatch):
    """The whole path with the real runner: a candidate graph still compiles
    and runs with its evaluators held back for the one scoring pass."""
    monkeypatch.setattr(tools_registry, 'find_tool', lambda name: {})
    monkeypatch.setattr(tools_registry, 'validate_tool_names', lambda names: None)
    calls = []

    def call(name, args, **kw):
        calls.append(name)
        return args['text'].upper()
    monkeypatch.setattr(tools_registry, 'call_tool', call)
    started = []
    real_start = runner.start_run
    monkeypatch.setattr(runner, 'start_run', lambda *a, **kw: started.append(real_start(*a, **kw)) or started[-1])
    g = {'id': 'replay', 'name': 'Replay', 'goal': 'g', 'flow_version': 3,
         'tasks': [{'name': 'work', 'kind': 'tool', 'tool': 'echo', 'inputs': [{'name': 'text', 'type': 'str', 'required': True}],
                    'outputs': [{'name': 'answer', 'type': 'str', 'required': True}]}],
         'edges': [], 'evaluators': [evaluator()]}
    state = {'task_id': 'real', 'execution_graph': copy.deepcopy(g)}
    canvas_evolution.execute(state, g, [{'text': 'hi', 'expected': 'HI'}, {'text': 'no', 'expected': 'other'}],
                             {'evaluator': 'quality', 'mode': 'evaluate', 'nodes': [], 'rounds': 1}, lambda s: None)
    assert calls == ['echo', 'echo']
    assert state['baseline']['metrics']['score'] == 0.5
    assert state['baseline']['metrics']['scored'] == 2
    # The replay's runs and batch belong to no workflow: gone once scored.
    assert len(started) == 2 and all(runner.get_run(i) is None for i in started)


class TestCandidateIsolation:
    """Each candidate starts empty, works on the workflow's files, and leaves
    nothing behind — a real workflow's stores are never touched."""

    def stores(self, tmp_path, monkeypatch):
        from backend.api import memory_store, table_store, workspace
        from backend.features.memory import stm_store
        monkeypatch.setattr(memory_store, 'MEMORY_DIR', tmp_path / 'memory')
        monkeypatch.setattr(table_store, 'TABLES_DIR', tmp_path / 'tables')
        monkeypatch.setattr(stm_store, 'STM_DIR', tmp_path / 'stm')
        monkeypatch.setattr(workspace, 'WORKSPACE_DIR', tmp_path / 'workspace')
        return memory_store, table_store, stm_store, workspace

    def test_candidate_stores_are_isolated_copied_and_removed(self, tmp_path, monkeypatch):
        memory_store, table_store, stm_store, workspace = self.stores(tmp_path, monkeypatch)
        real_files = workspace.files_dir('audit')
        real_files.mkdir(parents=True)
        (real_files / 'notes.txt').write_text('the workflow owns this')
        real_memory = memory_store.MEMORY_DIR / 'audit' / 'work'
        real_memory.mkdir(parents=True)
        ids, read = [], []

        def start(candidate, row, **kw):
            ids.append(candidate['id'])
            # What a tool of this run would be handed, and what it may write.
            files = workspace.files_dir(candidate['_workspace_id'])
            read.append((files / 'notes.txt').read_text())
            (files / 'written-by-a-candidate.txt').write_text('scratch')
            (memory_store.MEMORY_DIR / candidate['id'] / 'work').mkdir(parents=True, exist_ok=True)
            (stm_store.STM_DIR).mkdir(parents=True, exist_ok=True)
            (stm_store.STM_DIR / f"{candidate['id']}.json").write_text('{}')
            return kw['run_id']
        monkeypatch.setattr(runner, 'start_run', start)
        monkeypatch.setattr(runner, 'get_run', lambda id: {'status': 'success', 'result': {'answer': 'yes'},
                                                           'node_outputs': {'work': {'answer': 'yes'}}, 'nodes': []})
        monkeypatch.setattr(runner, '_make_llm', lambda **kw: FakeLLM())
        g = graph(evaluator())
        state = {'task_id': 'iso', 'execution_graph': copy.deepcopy(g)}
        canvas_evolution.execute(state, g, [{'text': 'q', 'expected': 'yes'}],
                                 {'evaluator': 'quality', 'mode': 'evolve_evaluate', 'nodes': ['work'], 'rounds': 1}, lambda s: None)
        assert len(set(ids)) == 2 and all(i.startswith('_evolve-iso-') for i in ids)
        assert read == ['the workflow owns this'] * 2   # candidates see the real files
        # Nothing the candidates wrote survives, and the workflow's own data is untouched.
        assert not list((memory_store.MEMORY_DIR).glob('_evolve-*'))
        assert not list((stm_store.STM_DIR).glob('_evolve-*'))
        assert not list((workspace.WORKSPACE_DIR).glob('_evolve-*'))
        assert real_memory.is_dir()
        assert (real_files / 'notes.txt').read_text() == 'the workflow owns this'
        assert not (real_files / 'written-by-a-candidate.txt').exists()

    def test_scratch_stores_are_cleaned_up_after_a_failure_and_a_restart(self, tmp_path, monkeypatch):
        from backend.api import evolve_api
        memory_store, table_store, stm_store, workspace = self.stores(tmp_path, monkeypatch)
        monkeypatch.setattr(evolve_api, 'EVOLVE_DIR', tmp_path / 'evolve')
        monkeypatch.setattr(evolve_api, '_tasks', {})
        from backend.api import batch
        # The replay itself fails (a failing record is only a failed record).
        monkeypatch.setattr(batch, 'start_batch', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('boom')))
        g = graph(evaluator())
        state = {'task_id': 'failing', 'execution_graph': copy.deepcopy(g)}
        (memory_store.MEMORY_DIR / '_evolve-failing-baseline').mkdir(parents=True)
        with pytest.raises(RuntimeError):
            canvas_evolution.execute(state, g, [{'text': 'q'}], {'evaluator': 'quality', 'mode': 'evaluate', 'nodes': [], 'rounds': 1}, lambda s: None)
        assert not list(memory_store.MEMORY_DIR.glob('_evolve-*'))
        # A task cut off by a restart: listing sweeps it, but spares a running one.
        for leftover in ('_evolve-old-baseline', '_evolve-live-baseline', 'real-workflow'):
            (memory_store.MEMORY_DIR / leftover).mkdir(parents=True)
        evolve_api._tasks['live'] = {'task_id': 'live', 'status': 'running', 'created_at': '2026-01-01'}
        evolve_api.list_tasks()
        assert sorted(p.name for p in memory_store.MEMORY_DIR.iterdir()) == ['_evolve-live-baseline', 'real-workflow']


class TestSharedMemoryIsRefused:
    def test_a_node_that_only_reads_mem0_is_refused_with_what_to_do(self):
        g = graph(evaluator())
        g['tasks'][0].update(use_long_term_memory=True, memory={'read_from': ['remember']})
        # The Mem0 node itself does not remember anything; 'work' only reads it.
        g['tasks'].append({'name': 'remember', 'prompt': 'p', 'inputs': [], 'outputs': [],
                           'memory': {'provider': 'mem0'}})
        with pytest.raises(sources.SourceError, match='Mem0') as error:
            canvas_evolution.prepare(g, {'evaluator': 'quality', 'mode': 'evaluate'})
        assert 'evaluate saved results' in str(error.value)
        assert "'work'" in str(error.value) and 'remember' in str(error.value)


class TestDirection:
    def test_an_evaluator_honours_the_chosen_direction(self, monkeypatch):
        report = evaluator_tools.report({'code': MATCH, 'metric': 'score', 'direction': 'minimize',
                                         'config': {}},
                                        [{'id': '0', 'status': 'success', 'inputs': {'expected': 'b'},
                                          'node_outputs': {'work': {'answer': 'a'}}}])
        assert report['objective'] == {'metric': 'score', 'direction': 'minimize'}
        fake_runs(monkeypatch)
        monkeypatch.setattr(runner, '_make_llm', lambda **kw: FakeLLM())
        g = graph(evaluator(direction='minimize'))
        state = {'task_id': 'dir', 'execution_graph': copy.deepcopy(g)}
        # The candidate answers 'yes' where the baseline answered 'no'; with a
        # minimized objective the worse-matching baseline is the one kept.
        canvas_evolution.execute(state, g, [{'text': 'q', 'expected': 'no'}],
                                 {'evaluator': 'quality', 'mode': 'evolve_evaluate', 'nodes': ['work'], 'rounds': 1}, lambda s: None)
        assert state['baseline']['metrics']['score'] == 1
        assert state['candidates'][0]['metrics']['score'] == 0 and state['candidates'][0]['accepted']
        assert state['optimized']['metrics']['score'] == 0
