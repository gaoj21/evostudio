"""Evolve holds out whole entities: prompts are proposed and chosen on dev,
and val — every record of the held-out entities — is scored once, at the
end, never seen by the proposer and never deciding anything."""
import copy

import pytest

from backend.api import runner
from backend.features.evaluation import canvas_evolution
from test_evaluator_evolve_audit import FakeLLM, _evaluator_worker_in_process, evaluator, graph  # noqa: F401

ENTITIES = [f'e{i}' for i in range(10)]
ROWS = [{'entity': c, 'text': f'{c}-{t}', 'expected': 'yes'} for t in ('t1', 't2') for c in ENTITIES]


@pytest.fixture
def entities(monkeypatch):
    """The Input declares each entity’s timeline as a trajectory."""
    from backend.features.execution import batch

    def assign(graph, pairs):
        for item, row in pairs:
            item['trajectory'] = row["entity"]
        return None
    monkeypatch.setattr(batch, 'assign_sequences', assign)


def replays(monkeypatch, answer):
    runs = {}

    def start(candidate, row, **kw):
        said = answer(candidate['tasks'][0]['prompt'], row["entity"])
        runs[kw['run_id']] = {'status': 'success', 'inputs': row, 'result': {'answer': said},
                              'node_outputs': {'work': {'answer': said}}, 'nodes': []}
        return kw['run_id']
    monkeypatch.setattr(runner, 'start_run', start)
    monkeypatch.setattr(runner, 'get_run', lambda id: runs.get(id))
    return runs


def evolve(monkeypatch, **extra):
    llm = FakeLLM()
    monkeypatch.setattr(runner, '_make_llm', lambda **kw: llm)
    g = graph(evaluator())
    state = {'task_id': 'hold', 'execution_graph': copy.deepcopy(g)}
    canvas_evolution.execute(state, g, copy.deepcopy(ROWS),
                             {'evaluator': 'quality', 'mode': 'evolve_evaluate', 'nodes': ['work'], 'rounds': 1, **extra},
                             lambda s: None)
    return state, llm


def val_entities(seed=0, fraction=0.3):
    units = ['t:' + r['entity'] for r in ROWS]
    return {u[2:] for u in canvas_evolution.held_out(units, fraction, seed)}


def test_whole_entities_are_held_out_and_never_shown_to_the_proposer(entities, monkeypatch):
    runs = replays(monkeypatch, lambda prompt, entity: 'yes' if prompt == 'new' else 'no')
    state, llm = evolve(monkeypatch)

    held = val_entities()
    assert {k: state['split'][k] for k in ('unit', 'dev_units', 'val_units', 'dev_records', 'val_records')} == \
        {'unit': 'trajectory', 'dev_units': 7, 'val_units': 3, 'dev_records': 14, 'val_records': 6}
    # Every candidate replays every record: memory may be read across entities.
    assert len(runs) == 2 * len(ROWS)
    proposal = '\n'.join(str(p) for p in llm.prompts)
    assert all(f'{c}-t' not in proposal for c in held)
    assert any(f'{c}-t' in proposal for c in set(ENTITIES) - held)
    v = state['validation']
    assert v['baseline']['score'] == 0 and v['optimized']['score'] == 1 and v['improved'] is True


def test_val_reports_but_never_decides(entities, monkeypatch):
    held = val_entities()
    # Better on dev, worse on val: still chosen — and val says it did not generalize.
    replays(monkeypatch, lambda prompt, entity: ('no' if entity in held else 'yes') if prompt == 'new'
            else ('yes' if entity in held else 'no'))
    state, _ = evolve(monkeypatch)

    assert state['candidates'][0]['accepted']
    assert state['baseline']['metrics']['score'] == 0 and state['optimized']['metrics']['score'] == 1
    assert state['validation']['improved'] is False
    assert state['validation']['baseline']['score'] == 1 and state['validation']['optimized']['score'] == 0


def test_the_split_is_fixed_by_the_seed():
    units = [f't:{c}' for c in ENTITIES] * 3
    assert canvas_evolution.held_out(units, 0.3, 0) == canvas_evolution.held_out(list(reversed(units)), 0.3, 0)
    assert canvas_evolution.held_out(units, 0.3, 0) != canvas_evolution.held_out(units, 0.3, 1)
    assert canvas_evolution.held_out(['t:only'] * 5) == set()      # one entity: nothing to hold out


def test_evaluation_only_scores_everything(entities, monkeypatch):
    replays(monkeypatch, lambda prompt, entity: 'yes')
    llm = FakeLLM()
    monkeypatch.setattr(runner, '_make_llm', lambda **kw: llm)
    g = graph(evaluator())
    state = {'task_id': 'all', 'execution_graph': copy.deepcopy(g)}
    canvas_evolution.execute(state, g, copy.deepcopy(ROWS),
                             {'evaluator': 'quality', 'mode': 'evaluate', 'nodes': [], 'rounds': 1}, lambda s: None)
    assert state['baseline']['metrics']['score'] == 1
    assert len(state['baseline']['records']) == len(ROWS)
    assert 'split' not in state and 'validation' not in state
