"""Exercise Schedule through the real streaming Dataset and batch engines."""
import copy
from datetime import datetime
from unittest.mock import Mock

import pytest
from test_dataloader_resilience import env  # shared real Dataset + fake model fixture
from backend.api import graphs, runner, batch
from backend.features.execution import scheduler
from backend.features.data import input_composition, dataset_stream
from backend.features import persistence


@pytest.fixture
def scheduled(env, tmp_path, monkeypatch):
    graph, behaviour = env
    monkeypatch.setattr(scheduler, 'SCHEDULES_DIR', tmp_path / 'schedules')
    monkeypatch.setattr(scheduler, '_start_thread', Mock())
    monkeypatch.setattr(graphs, 'load_graph', lambda _: graph)
    monkeypatch.setattr(scheduler, '_now', lambda: datetime.fromisoformat('2026-09-21T10:00:00+08:00'))
    scheduler.set_schedule(graph, {'mode': 'weekly', 'weekday': 0, 'time_input': 'period_at'})
    state = scheduler.load(graph['id'])
    state['next_fire'] = '2026-09-21T09:00:00+08:00'
    scheduler._save(graph['id'], state)
    yield graph, behaviour
    for bid in list(batch._threads):
        assert batch.wait_for(bid, timeout=30)


def fire(graph):
    scheduler._fire(graph['id'], scheduler.load(graph['id']))
    return scheduler.load(graph['id'])['last_run_id']


def test_schedule_processes_all_records_and_tail_with_original_time(scheduled):
    graph, seen = scheduled
    # Three records with a batch size of two: the last batch must also run.
    graph['tasks'][0]['source']['n'] = 3
    state = scheduler.load(graph['id'])
    state['execution_snapshot']['graph'] = copy.deepcopy(graph)
    code = state['execution_snapshot']['graph']['tasks'][0]['source']['code']
    code = code.replace('def build_dataset(resource):', '''def build_dataset(resource, config):
    assert config['period_at'] == '2026-09-21T09:00:00+08:00'
    assert config['run_context']['scheduled_at'] == config['period_at']''')
    state['execution_snapshot']['graph']['tasks'][0]['source']['code'] = code
    scheduler._save(graph['id'], state)
    bid = fire(graph)
    assert batch.wait_for(bid, timeout=30)
    result = batch.get_batch(bid)
    assert result['status'] == 'succeeded', result.get('error')
    assert seen['seen'] == [0, 1, 2]
    assert result['total'] == 3 and result['collection_complete']
    for item in result['items']:
        run = runner.get_run(item['run_id'])
        assert run['session'] == state['session']
        assert run['inputs']['period_at'] == state['next_fire']
    fire(graph)
    assert scheduler.load(graph['id'])['next_fire'].startswith('2026-09-28')


def test_failed_occurrence_resumes_same_batch_with_saved_graph(scheduled):
    graph, behaviour = scheduled
    behaviour['fail'].add(2)
    bid = fire(graph)
    assert batch.wait_for(bid, timeout=30)
    fire(graph)
    before = scheduler.load(graph['id'])
    assert before['needs_resume'] and before['next_fire'].startswith('2026-09-21')
    successful = {i['index']: i['run_id'] for i in batch.get_batch(bid)['items'] if i['status'] == 'success'}
    graph['tasks'][1]['prompt'] = 'changed prompt without required bindings'
    graph['tasks'][0]['source']['code'] = 'raise RuntimeError("new code must not run")'
    behaviour['fail'].clear(); behaviour['seen'].clear()
    scheduler.resume(graph['id'], 'all')
    assert fire(graph) == bid
    assert batch.wait_for(bid, timeout=30)
    result = batch.get_batch(bid)
    assert result['status'] == 'succeeded', result.get('error')
    assert behaviour['seen'] == [2, 4, 6]
    assert all(result['items'][i]['run_id'] == rid for i, rid in successful.items())
    assert result['execution_snapshot']['graph']['tasks'][1]['prompt'] != graph['tasks'][1]['prompt']
    fire(graph)
    assert scheduler.load(graph['id'])['last_completed_due'] == before['next_fire']


def test_single_record_read_is_bounded_and_closes_stream(monkeypatch):
    seen, closed = [], []
    def chunks(config, cancelled):
        seen.append(config)
        try:
            yield [{'x': 4}]
            raise AssertionError('Must not consume the rest of the dataset')
        finally:
            closed.append(True)
    monkeypatch.setattr(dataset_stream, 'chunks', chunks)
    node = {'name': 'input', 'source': {'type': 'dataloader', 'loader': 'python', 'offset': 2, 'n': 10}}
    row = input_composition.read_record({'tasks': []}, node, 4, {'period_at': 'Monday'})
    assert row == {'x': 4} and closed == [True]
    assert seen[0]['offset'] == 6 and seen[0]['n'] == seen[0]['read_batch_size'] == 1
    assert seen[0]['reader_config']['period_at'] == 'Monday'


def test_failed_atomic_rewrite_preserves_completed_run(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'RUNS_DIR', tmp_path)
    state = {'run_id': 'saved', 'status': 'success', 'nodes': [], 'result': {'value': 1}}
    assert runner._persist_run(state)
    def broken_dump(value, stream, **kwargs):
        stream.write('{"status":')
        raise OSError('simulated disk failure')
    monkeypatch.setattr(persistence.json, 'dump', broken_dump)
    assert not runner._persist_run({**state, 'result': {'value': 2}})
    assert runner._read_run('saved')['result'] == {'value': 1}
    assert not list(tmp_path.glob('*.tmp'))


def test_run_does_not_execute_when_initial_persistence_fails(monkeypatch):
    execute = Mock()
    monkeypatch.setattr(runner, '_persist_run', lambda _: False)
    monkeypatch.setattr(runner, '_execute_run', execute)
    with pytest.raises(OSError, match='not started'):
        runner.start_run({'id': 'no-disk', 'tasks': []}, {}, background=False)
    execute.assert_not_called()


def test_crash_after_run_success_before_batch_checkpoint_does_not_replay(scheduled):
    graph, behaviour = scheduled
    bid = fire(graph)
    assert batch.wait_for(bid, timeout=30)
    state = batch.get_batch(bid)
    old_ids = [i['run_id'] for i in state['items']]
    state['status'] = 'interrupted'
    state['items'][-1]['status'] = 'pending'
    batch._persist_batch(state)
    batch._batches.pop(bid, None)
    behaviour['seen'].clear()
    scheduler.resume(graph['id'], 'all')
    assert fire(graph) == bid
    assert batch.wait_for(bid, timeout=30)
    recovered = batch.get_batch(bid)
    assert recovered['status'] == 'succeeded'
    assert behaviour['seen'] == []
    assert [i['run_id'] for i in recovered['items']] == old_ids


def test_single_run_never_reads_a_second_record(env):
    graph, behaviour = env
    graph['tasks'][0]['source']['code'] = '''from torch.utils.data import IterableDataset
class Rows(IterableDataset):
    def __iter__(self):
        yield {'id': 0, 'group': 'a', 'value': 0, 'day': '2026-09-21'}
        raise RuntimeError('Reading the whole dataset is forbidden')
def build_dataset(resource):
    return Rows()
'''
    rid = runner.start_run(graph, {}, background=False)
    result = runner.get_run(rid)
    assert result['status'] == 'success', result.get('error')
    assert behaviour['seen'] == [0]


def test_batch_snapshot_survives_later_canvas_edits(env):
    from backend.features.execution import loader_run
    graph, behaviour = env
    behaviour['fail'].add(2)
    bid = loader_run.start(graph, graph['tasks'][0], {'workers': 1})['batch_id']
    assert batch.wait_for(bid, timeout=30)
    graph['tasks'][1]['name'] = 'different'
    graph['tasks'][0]['source']['code'] = 'not Python'
    behaviour['fail'].clear(); behaviour['seen'].clear()
    assert batch.resume_batch(bid, graph)['resumed']
    assert batch.wait_for(bid, timeout=30)
    assert batch.get_batch(bid)['status'] == 'succeeded'
    assert behaviour['seen'] == [2, 4, 6]


def test_schedule_waits_until_success_is_durable(tmp_path, monkeypatch):
    from backend.features.execution.scheduled_execution import get_execution
    monkeypatch.setattr(runner, 'RUNS_DIR', tmp_path)
    state = {'run_id': 'settling', 'status': 'success', 'nodes': [], '_executing': True}
    monkeypatch.setitem(runner._runs, 'settling', state)
    assert get_execution({'run_id': 'settling'})['status'] == 'running'
    assert runner._persist_run(state)
    state['_executing'] = False
    assert get_execution({'run_id': 'settling'})['status'] == 'success'


def test_failed_batch_rewrite_preserves_original_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(batch, 'BATCHES_DIR', tmp_path)
    state = {'batch_id': 'durable', 'status': 'running', 'items': []}
    batch._persist_batch(state)
    def broken_dump(value, stream, **kwargs):
        stream.write('{')
        raise OSError('simulated disk failure')
    monkeypatch.setattr(persistence.json, 'dump', broken_dump)
    with pytest.raises(OSError):
        batch._persist_batch({**state, 'status': 'succeeded'})
    assert batch._read_batch('durable')['status'] == 'running'
    assert not list(tmp_path.glob('*.tmp'))
