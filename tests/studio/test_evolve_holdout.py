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


def test_a_candidate_replays_as_a_node_by_node_batch_and_leaves_nothing_behind(entities, monkeypatch):
    """Not one record after another: a batch, node by node in the Input's
    own chunks, the records of a chunk at once — then removed."""
    from backend.api import batch
    replays(monkeypatch, lambda prompt, entity: 'yes')
    real_start, started = batch.start_batch, []

    def spy(graph, records, source, **kw):
        batch_id = real_start(graph, records, source, **kw)
        started.append((batch_id, len(records), source, kw))
        return batch_id
    monkeypatch.setattr(batch, 'start_batch', spy)
    state, _ = evolve(monkeypatch, source={'type': 'canvas', 'config': {'type': 'dataloader', 'read_batch_size': 5}},
                      workers=3)

    assert len(started) == 2                                    # baseline + one candidate
    for batch_id, count, source, kw in started:
        assert count == len(ROWS) and kw['mode'] == 'node' and kw['workers'] == 3
        assert source['config']['read_batch_size'] == 5          # chunks are the Input's batches
        assert batch.get_batch(batch_id) is None                 # removed with its runs
    assert 'current_batch' not in state or state['current_batch'] is None


def test_starting_an_evolve_does_not_wait_for_the_input(monkeypatch, tmp_path):
    """The request answers at once; the task reads the Input as its first stage."""
    import threading
    from fastapi.testclient import TestClient
    from backend.api import app, graphs
    from backend.features.evaluation import evolve_api
    g = graph(evaluator())
    monkeypatch.setattr(graphs, 'load_graph', lambda _: copy.deepcopy(g))
    monkeypatch.setattr(graphs, 'validate_graph', lambda _: (g['tasks'], []))
    monkeypatch.setattr(evolve_api, 'EVOLVE_DIR', tmp_path / 'evolve')
    monkeypatch.setattr(evolve_api, '_tasks', {})
    reading, release = threading.Event(), threading.Event()

    def load_rows(graph):
        reading.set()
        assert release.wait(10)
        return [{'text': 'q', 'expected': 'yes'}]
    monkeypatch.setattr(canvas_evolution, 'load_rows', load_rows)
    from backend.features.data import input_composition
    monkeypatch.setattr(input_composition, 'primary', lambda graph: {'name': 'input', 'source': {'type': 'dataloader'}})
    monkeypatch.setattr(canvas_evolution, 'execute', lambda state, graph, rows, params, stage: state.update(seen=len(rows)))

    response = TestClient(app.app).post('/api/graphs/audit/evolve', json={'source': 'canvas', 'evaluator': 'quality', 'mode': 'evaluate'})
    assert response.status_code == 200, response.text
    task_id = response.json()['task_id']
    assert reading.wait(5)
    assert evolve_api.get_task(task_id)['stage'] == 'reading the Input'
    release.set()
    for _ in range(200):
        task = evolve_api.get_task(task_id)
        if task['status'] != 'running':
            break
        import time; time.sleep(0.02)
    assert task['status'] == 'done' and task['params']['n_dev'] == 1


DATED = [{'entity': f'e{i}', 'as_of': f'2025-0{m}-01', 'text': f'e{i}-{m}', 'expected': 'yes'}
         for m in range(1, 6) for i in range(4)]


def test_hold_out_the_latest_values_of_a_chosen_field(monkeypatch):
    """Split on time: dev is the earlier dates, val the latest — every
    entity is on both sides, and each date wholly on one."""
    runs = replays(monkeypatch, lambda prompt, entity: 'yes' if prompt == 'new' else 'no')
    llm = FakeLLM()
    monkeypatch.setattr(runner, '_make_llm', lambda **kw: llm)
    g = graph(evaluator())
    state = {'task_id': 'dated', 'execution_graph': copy.deepcopy(g)}
    canvas_evolution.execute(state, g, copy.deepcopy(DATED),
                             {'evaluator': 'quality', 'mode': 'evolve_evaluate', 'nodes': ['work'], 'rounds': 1,
                              'split_field': 'as_of', 'split_order': 'latest', 'val_fraction': 0.4}, lambda s: None)
    split = state['split']
    assert (split['field'], split['order'], split['dev_units'], split['val_units']) == ('as_of', 'latest', 3, 2)
    assert split['val_records'] == 8
    proposal = '\n'.join(str(p) for p in llm.prompts)
    assert 'e0-1' in proposal and 'e0-4' not in proposal and 'e0-5' not in proposal
    assert len(runs) == 2 * len(DATED)                  # every record is still replayed


def test_split_on_any_field_at_random():
    units, kind = canvas_evolution.split_units({}, DATED, 'entity')
    assert kind == 'entity' and units[:4] == ['e0', 'e1', 'e2', 'e3']
    held = canvas_evolution.held_out(units, 0.25, 0)
    assert len(held) == 1 and held <= {'e0', 'e1', 'e2', 'e3'}
    assert canvas_evolution.held_out([3, 10, 2, 1], 0.5, order='latest') == {3, 10}   # numbers by value


def test_a_split_field_every_record_must_have():
    rows = DATED + [{'entity': 'e9', 'text': 'no date'}]
    with pytest.raises(canvas_evolution.SourceError, match="1 of 21 records have no 'as_of'"):
        canvas_evolution.split_units({}, rows, 'as_of')


def test_the_split_settings_are_checked(monkeypatch):
    from backend.features.data import input_composition
    monkeypatch.setattr(input_composition, 'primary', lambda graph: {'name': 'input', 'source': {'type': 'dataloader'}})
    g = graph(evaluator())
    body = {'evaluator': 'quality', 'mode': 'evolve_evaluate', 'nodes': ['work']}
    assert canvas_evolution.settings(g, {**body, 'split_field': 'as_of', 'split_order': 'latest'})['split_field'] == 'as_of'
    with pytest.raises(canvas_evolution.SourceError, match='needs a field'):
        canvas_evolution.settings(g, {**body, 'split_order': 'latest'})
    with pytest.raises(canvas_evolution.SourceError, match='at random or the latest'):
        canvas_evolution.settings(g, {**body, 'split_field': 'as_of', 'split_order': 'sideways'})
