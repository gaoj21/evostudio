"""Persistent scheduled experiments, tested with a clock and no model calls."""
from datetime import datetime
from unittest.mock import Mock
import pytest
from backend.features.execution import scheduler as sched


@pytest.fixture
def clock(tmp_path, monkeypatch):
    from backend.api import graphs
    monkeypatch.setattr(sched, 'SCHEDULES_DIR', tmp_path)
    monkeypatch.setattr(sched, '_start_thread', Mock())
    monkeypatch.setattr(graphs, 'load_graph', lambda gid: {'id': gid, 'tasks': []})
    now = [datetime.fromisoformat('2026-09-20T12:00:00+08:00')]
    monkeypatch.setattr(sched, '_now', lambda: now[0])
    runs = {}
    def start(graph, inputs, run_id, **kw):
        runs[run_id] = {'status': 'running', 'inputs': inputs, **kw}
        return run_id
    monkeypatch.setattr(sched.runner, 'start_run', Mock(side_effect=start))
    monkeypatch.setattr(sched.runner, 'get_run', lambda rid: runs.get(rid))
    return now, runs


def setup():
    return sched.set_schedule({'id': 'g'}, {'mode': 'weekly', 'weekday': 0, 'time': '09:00', 'timezone': 'Asia/Singapore'})


def complete(now, runs):
    sched._fire('g', sched.load('g'))
    current = sched.load('g')
    runs[current['last_run_id']]['status'] = 'success'
    sched._fire('g', current)


def test_completed_monday_waits_for_next_monday_and_preserves_identity(clock):
    now, runs = clock
    initial = setup()
    now[0] = datetime.fromisoformat('2026-09-21T09:00:00+08:00')
    complete(now, runs)
    done = sched.load('g')
    assert done['enabled'] and done['next_fire'] == '2026-09-28T09:00:00+08:00'
    assert not done['in_flight']
    now[0] = datetime.fromisoformat('2026-09-23T10:00:00+08:00')
    sched.set_schedule({'id': 'g'}, {'enabled': False})
    now[0] = datetime.fromisoformat('2026-09-28T10:00:00+08:00')
    resumed = sched.resume('g', 'all')
    assert resumed['experiment_id'] == initial['experiment_id']
    assert resumed['session'] == initial['session']
    assert resumed['next_fire'] == done['next_fire']
    assert sched.runner.start_run.call_count == 1  # never re-run last Monday
    complete(now, runs)
    assert sched.load('g')['next_fire'] == '2026-10-05T09:00:00+08:00'


@pytest.mark.parametrize('policy,expected', [('all', '2026-09-21'), ('latest', '2026-10-12'), ('skip', '2026-10-19')])
def test_three_missed_weeks_are_user_selectable(clock, policy, expected):
    now, _ = clock
    original = setup()
    now[0] = datetime.fromisoformat('2026-10-14T12:00:00+08:00')
    assert sched.restore() == []  # default asks instead of spending silently
    assert sched.status('g')['needs_resume']
    out = sched.resume('g', policy)
    assert out['next_fire'].startswith(expected)
    assert out['session'] == original['session']


def test_catch_up_uses_original_due_times_not_resume_time(clock):
    now, runs = clock
    setup()
    sched.set_schedule({'id': 'g'}, {'time_input': 'period_at'})
    now[0] = datetime.fromisoformat('2026-10-01T12:00:00+08:00')
    sched.resume('g', 'all')
    complete(now, runs); complete(now, runs)
    dates = [r['inputs']['period_at'] for r in runs.values()]
    assert dates == ['2026-09-21T09:00:00+08:00', '2026-09-28T09:00:00+08:00']
    assert sched.load('g')['next_fire'] == '2026-10-05T09:00:00+08:00'


def test_running_occurrence_does_not_overlap_and_failure_requires_resume(clock):
    now, runs = clock
    setup(); sched._fire('g', sched.load('g'))
    pending = sched.load('g')
    sched._fire('g', pending)
    assert sched.runner.start_run.call_count == 1
    runs[pending['last_run_id']]['status'] = 'failed'
    sched._fire('g', pending)
    assert sched.load('g')['needs_resume']
    sched.resume('g', 'all'); sched._fire('g', sched.load('g'))
    assert sched.load('g')['last_run_id'] == pending['last_run_id']


def test_crash_after_run_success_before_cursor_save_does_not_repeat(clock):
    now, runs = clock
    setup(); sched._fire('g', sched.load('g'))
    pending = sched.load('g')
    runs[pending['last_run_id']]['status'] = 'success'
    now[0] = datetime.fromisoformat('2026-09-23T10:00:00+08:00')
    sched.restore()
    assert sched.load('g')['next_fire'].startswith('2026-09-28')
    assert sched.runner.start_run.call_count == 1


def test_crash_before_dispatch_retains_claim(clock):
    now, runs = clock
    setup()
    sched.runner.start_run.side_effect = RuntimeError('offline')
    sched._fire('g', sched.load('g'))
    interrupted = sched.load('g')
    assert interrupted['in_flight'] and interrupted['needs_resume']
    original = interrupted['next_fire']
    sched.resume('g', 'all')
    assert sched.load('g')['next_fire'] == original


def test_resume_endpoint_requires_a_strategy_and_keeps_identity(clock, monkeypatch):
    from fastapi.testclient import TestClient
    from backend.api import app, graphs
    monkeypatch.setattr(graphs, 'graph_exists', lambda _: True)
    original = setup()
    client = TestClient(app.app)
    assert client.post('/api/graphs/g/schedule/resume', json={}).status_code == 422
    response = client.post('/api/graphs/g/schedule/resume', json={'recovery_policy': 'skip'})
    assert response.status_code == 200
    assert response.json()['experiment_id'] == original['experiment_id']


def test_interval_resume_preserves_phase_after_long_outage(clock):
    now, _ = clock
    sched.set_schedule({'id': 'g'}, {'mode': 'interval', 'interval_minutes': 5})
    original = sched.load('g')['next_fire']
    now[0] = datetime.fromisoformat('2036-09-20T12:03:00+08:00')
    result = sched.resume('g', 'skip')
    assert result['next_fire'] == '2036-09-20T12:05:00+08:00'
    assert original == '2026-09-20T12:05:00+08:00'


def test_resume_choice_does_not_silently_enable_future_automatic_replays(clock):
    setup()
    result = sched.resume('g', 'all')
    assert result['recovery_policy'] == 'ask'
    assert result['last_recovery_policy'] == 'all'


def test_legacy_paused_schedule_recovers_cursor_from_last_fire(clock):
    now, _ = clock
    setup()
    legacy = sched.load('g')
    legacy.update(enabled=False, next_fire=None, last_fire='2026-09-21T09:00:00+08:00')
    sched._save('g', legacy)
    now[0] = datetime.fromisoformat('2026-09-30T10:00:00+08:00')
    assert sched.resume('g', 'all')['next_fire'] == '2026-09-28T09:00:00+08:00'
