"""The saved monitoring workflow must receive dated evidence and real decisions."""
import json
from pathlib import Path

import pytest

from backend.api import graphs, memory_policy, table_store


@pytest.fixture
def graph():
    from backend.api.studio_config import DEFAULT_DATA_DIR
    path = DEFAULT_DATA_DIR / 'graphs' / 'credit-risk-monitoring.json'
    return json.loads(path.read_text())


def test_observation_date_and_filings_reach_detection(graph):
    graphs.validate_graph(graph)
    bindings = graphs.compile_bindings(graph['tasks'], graph['edges'])
    for name in ['source_news', 'detect']:
        task = next(t for t in graph['tasks'] if t['name'] == name)
        assert '{as_of}' in task['prompt']
        assert '{window_end}' not in task['prompt']
        assert 'as_of' in bindings[name]
        edge = next(e for e in graph['edges'] if e['source'] == 'feed' and e['target'] == name)
        assert {'from': 'as_of', 'to': 'as_of'} in edge['mappings']
    detect = next(t for t in graph['tasks'] if t['name'] == 'detect')
    assert '{filing_batch}' in detect['prompt']
    assert 'filing_batch' in bindings['detect']


@pytest.mark.asyncio
async def test_investigation_reads_only_this_companys_earlier_decisions(graph, tmp_path, monkeypatch):
    monkeypatch.setattr(table_store, 'TABLES_DIR', tmp_path / 'tables')
    task = next(t for t in graph['tasks'] if t['name'] == 'investigate')
    for subject, at, decision in [
        ('Acme', '2025-01-01', 'confirmed unresolved default'),
        ('Other', '2025-01-01', 'other company secret'),
        ('Acme', '2025-02-01', 'same day decision'),
        ('Acme', '2025-03-01', 'future decision'),
    ]:
        table_store.upsert(graph['id'], 'decide', subject, at,
                           {'inputs': {'company': subject, 'as_of': at},
                            'outputs': {'decision': decision}}, '2026-09-09')
    block = await memory_policy.recall_async({}, task, {'company': 'Acme'},
        run_data={'as_of': '2025-02-01'}, table=table_store, graph_id=graph['id'])
    assert 'confirmed unresolved default' in block
    assert '(from decide)' in block
    for excluded in ['other company secret', 'same day decision', 'future decision']:
        assert excluded not in block
    assert memory_policy.policy(task)['session_recall'] == 0


def test_table_recall_preserves_alerts_after_a_long_profile(graph):
    task = next(t for t in graph['tasks'] if t['name'] == 'investigate')
    context = json.dumps({'profile': 'background ' * 65,
                          'past_alerts': 'CONFIRMED UNRESOLVED MISSED PAYMENT', 'cases': 'None'})
    block = memory_policy.table_block([
        {'at': '2025-01-01', 'payload': {'outputs': {'context': context}}}
    ], task, 'Acme', '2025-02-01')
    assert 'CONFIRMED UNRESOLVED MISSED PAYMENT' in block


def test_memory_is_dated_by_as_of_and_needs_no_window(graph):
    """The risk pipeline dates everything by as_of. Its memory must work on a
    feed record that carries no window_start/window_end at all."""
    for name in ('investigate', 'decide'):
        task = next(t for t in graph['tasks'] if t['name'] == name)
        memory = task['memory']
        assert memory['at'] == 'as_of' and memory['match'] == 'company'
        assert not [f for f in memory.get('context') or [] if f.startswith('window')]
        payload = memory_policy.select(
            task, {'company': 'Acme'}, {task['outputs'][0]['name']: '{"x": 1}'},
            run_data={'company': 'Acme', 'as_of': '2025-03-01',
                      'detection': '{"source": "news"}'})
        assert 'unbound' not in payload
        assert memory_policy.table_write(task, payload)['at'].startswith('2025-03-01')


def test_presets_and_api_sources_speak_as_of():
    from credit_risk.studio.presets import credit_risk_presets
    from backend.api import registry
    from backend.features.data.source_apis import all_source_types
    SOURCE_TYPES = all_source_types()
    # The presets reach the palette through the plugin, grouped under it.
    palette = {p['type']: p for p in registry.node_presets()}
    for preset in credit_risk_presets():
        assert palette[preset['type']]['group'] == 'Credit risk'
    for preset in credit_risk_presets():
        d = preset['defaults']
        names = [i['name'] for i in d.get('inputs') or []]
        assert not [n for n in names if n.startswith('window')], preset['type']
        assert '{window_end}' not in d['prompt'] and '{window_start}' not in d['prompt']
    detect = next(p for p in credit_risk_presets() if p['type'] == 'cr_detect')
    assert 'as_of' in [i['name'] for i in detect['defaults']['inputs']]
    for kind in ('credit_risk', 'gdelt_news', 'sec_edgar_8k'):
        assert 'as_of' in SOURCE_TYPES[kind]['outputs'], kind
