"""Generic workflows: no company/window schema, exact metadata, append identity."""
import asyncio
import json
import sqlite3
import pytest
from backend.features.memory import bindings, memory_policy as policy, table_store as store


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'TABLES_DIR', tmp_path)
    store.forget()


def task(**config):
    return {'name': 'summarize', 'use_long_term_memory': True,
            'inputs': [{'name': 'text'}], 'outputs': [{'name': 'summary'}],
            'memory': {'version': 2, 'kind': 'table', 'inputs': [],
                       'outputs': ['summary'], 'time_filter': False, **config}}


def test_upstream_time_is_metadata_even_when_content_excludes_inputs():
    t = task(at='nodes.loader.outputs.as_of', match='nodes.loader.outputs.id')
    siblings = {'summarize': t, 'loader': {'outputs': [{'name': 'as_of'}, {'name': 'id'}]}}
    assert policy.validate(t, siblings) == []
    data = bindings.runtime_context({'_node_io': {'loader': {'output': {'as_of': '2026-09-01', 'id': 'a'}}}})
    payload = policy.select(t, {'text': 'hello'}, {'summary': 'ok'}, run_data=data)
    assert payload['inputs'] == {}
    assert payload['metadata']['subject'] == 'a'
    assert payload['metadata']['event_time'].startswith('2026-09-01T')
    assert policy.table_write(t, payload)['at'].startswith('2026-09-01T')


def test_no_time_or_entity_reads_recent_records_and_retries_do_not_duplicate():
    t = task()
    for run, value in [('1', 'a'), ('2', 'b'), ('2', 'b')]:
        row = policy.table_write(t, policy.select(t, {}, {'summary': value}))
        store.write('g', 'summarize', row['subject'], row['at'], row['payload'], f'2026-09-0{run}', execution_id=run)
    assert store.count('g', 'summarize') == 2
    text = asyncio.run(policy.recall_async({}, t, {}, table=store, graph_id='g'))
    assert 'summary: a' in text and 'summary: b' in text


def test_two_events_same_entity_same_day_are_preserved():
    for run in ('1', '2'):
        store.write('g', 'n', 'a', '2026-09-01', {'outputs': {'event': run}}, run, execution_id=run)
    assert len(store.before('g', 'n', 'a', '2026-09-02')) == 2


def test_explicit_upsert_key_updates_across_dates():
    for day in ('01', '02'):
        store.write('g', 'n', 'a', f'2026-09-{day}', {'outputs': {'event': day}}, day, mode='upsert', key='ticket-1')
    rows = store.rows('g', 'n')
    assert len(rows) == 1 and rows[0]['at'] == '2026-09-02'
    assert policy.validate(task(write_mode='upsert'), {'summarize': task()})


def test_time_filter_cannot_fall_back_to_unrestricted_read():
    t = task(at='as_of', time_filter=True)
    with pytest.raises(bindings.BindingError, match="dated by 'as_of'.*reading was skipped"):
        asyncio.run(policy.recall_async({}, t, {}, table=store, graph_id='g'))
    assert policy.read_cutoff(task(at='as_of', time_filter=False), {}) == ''


def test_timezone_equivalence_and_undated_records():
    store.write('g', 'n', 'a', '2026-09-01T10:00:00+08:00', {}, 'now', execution_id='1')
    store.write('g', 'n', 'a', '', {}, 'now', execution_id='2')
    assert store.before('g', 'n', 'a', '2026-09-01T02:00:00Z') == []
    assert len(store.before('g', 'n', 'a', '2026-09-01T02:00:01Z')) == 1
    assert not policy._earlier('2026-09-01T10:00:00+08:00', '2026-09-01T02:00:00Z')


def test_qualified_binding_does_not_use_same_named_field_from_other_node():
    data = {'as_of': 'wrong', 'nodes.other.outputs.as_of': 'also wrong'}
    assert bindings.resolve(data, 'nodes.loader.outputs.as_of') is None
    data['nodes.loader.outputs.record'] = json.dumps({'as_of': 'right'})
    assert bindings.resolve(data, 'nodes.loader.outputs.record.as_of') == 'right'


def test_old_database_migrates_preserving_records_and_legacy_upserts():
    path = store.path('g', 'n'); path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE rows_ (subject TEXT, at TEXT, recorded_at TEXT, payload TEXT, PRIMARY KEY(subject, at))')
        db.execute('INSERT INTO rows_ VALUES (?, ?, ?, ?)', ('a', '2026-01-01', 'then', '{"outputs":{"summary":"old"}}'))
    assert store.count('g', 'n') == 1
    store.upsert('g', 'n', 'a', '2026-01-01', {'outputs': {'summary': 'updated'}}, 'now')
    store.write('g', 'n', 'a', '2026-01-01', {}, 'now', execution_id='new')
    assert store.count('g', 'n') == 2
    assert any(r['payload'].get('outputs', {}).get('summary') == 'updated' for r in store.rows('g', 'n'))


def test_mem0_unsupported_filters_are_reported_not_discarded():
    errors = policy.validate(task(provider='mem0', at='as_of'))
    assert any('Mem0 adapter' in e for e in errors)


def test_old_window_context_does_not_block_a_pipeline_that_only_has_as_of():
    t = task(at='as_of', context=['window_start', 'window_end'], time_filter=True)
    siblings = {'summarize': t, 'loader': {'outputs': [{'name': 'as_of'}]}}
    assert policy.validate(t, siblings) == []
    payload = policy.select(t, {}, {'summary': 'ok'}, run_data={'as_of': '2026-09-01'})
    assert payload['metadata']['event_time'].startswith('2026-09-01')
    assert not payload['inputs']
    resolved = policy.with_context(t, {}, {'as_of': '2026-09-02'})
    assert policy.read_cutoff(t, resolved).startswith('2026-09-02')
