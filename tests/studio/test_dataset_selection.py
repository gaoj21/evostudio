import json
from pathlib import Path
import pytest

DATASET = '2026-09-10-random-dev-test-v1'


def test_catalog_and_feed_read_selected_split_without_outcomes():
    from backend.api import sources, datasets
    info = sources.credit_risk_info(DATASET)
    assert info['splits'] == {'dev':64, 'test':43}
    records = sources.credit_risk_records(dataset=DATASET, split='test', n=0)
    assert len(records) == 707
    assert len({r['sample_id'] for r in records}) == 43
    assert len({r['cik'] for r in records}) == 40
    expected = {r['case_id'] for r in datasets.rows(datasets.ROOT / DATASET / 'test' / 'cases.jsonl')}
    assert {r['sample_id'] for r in records} == expected
    for r in records:
        sample = json.loads(r['sample_json'])
        assert not {'label', 'outcome', 'reviewed_event', 'event_date'} & sample.keys()
        assert all(n['date'] <= r['as_of'] for n in sample['news'])
    assert records == sources.records_from_source_node({'source': {'type':'credit_risk', 'dataset':DATASET, 'split':'test', 'n':0}})


def test_evolve_joins_separate_labels_and_excludes_unknown_outcomes():
    from backend.api import sources, datasets
    records = sources.credit_risk_records(dataset=DATASET, split='dev', n=0, with_labels=True)
    assert len(records) == sources.credit_risk_info(DATASET)['evolve_splits']['dev']
    for r in records:
        assert r['label']['type'] == 'positive'
        assert r['label']['event_date'] > r['inputs']['window_end']
        assert 'label' not in json.loads(r['inputs']['sample_json'])
        assert 'event_date' not in r['inputs']
    with pytest.raises(sources.SourceError):
        sources.credit_risk_records(dataset='../../elsewhere')
    with pytest.raises(sources.SourceError):
        sources.credit_risk_records(dataset=DATASET, split='train')


def test_endpoints_preserve_dataset_selection(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from backend.api import app, graphs, evolve_api
    from conftest import make_graph, make_task
    monkeypatch.setattr(graphs, 'GRAPHS_DIR', tmp_path / 'graphs')
    node=make_task('feed',outputs=['company','news_batch'])
    node.update(kind='source',source={'type':'credit_risk','dataset':DATASET,'split':'test','n':0})
    graph=make_graph([node,make_task('judge',inputs=['company','news_batch'],outputs=['verdict'])],edges=[('feed','judge')],id='data-choice')
    graphs.save_graph('data-choice',graph)
    client=TestClient(app.app)
    response=client.post('/api/graphs/data-choice/run-batch/preview',json={'source':'canvas'})
    assert response.status_code==200, response.text
    assert response.json()['total']==707
    assert response.json()['source']['dataset']==DATASET
    assert client.post('/api/graphs/data-choice/run-batch/preview',json={'source':'canvas','metric':'credit_risk'}).status_code==422
    captured=[]
    monkeypatch.setattr(evolve_api,'start_evolve',lambda g,r,m,p: captured.append((r,p)) or 'chosen')
    response=client.post('/api/graphs/data-choice/evolve',json={'source':'credit_risk','dataset':DATASET,'split':'test','n':3,'nodes':['judge']})
    assert response.status_code==200,response.text
    assert len(captured[0][0])==3
    assert captured[0][1]['source']['dataset']==DATASET


def test_release_window_steps_preserve_all_evidence_without_future_data():
    from backend.api import sources
    from collections import Counter, defaultdict
    daily = sources.credit_risk_records(dataset=DATASET, split='test', n=0, step='daily')
    expected = Counter((r['sample_id'], d) for r in daily for d in r['document_ids'])
    counts = {}
    for step in ('none', 'weekly', 'monthly'):
        records = sources.credit_risk_records(dataset=DATASET, split='test', n=0, step=step)
        counts[step] = len(records)
        assert Counter((r['sample_id'], d) for r in records for d in r['document_ids']) == expected
        by_case = defaultdict(list)
        for r in records:
            by_case[r['sample_id']].append(r['as_of'])
            assert r['window_start'] <= r['as_of'] <= r['window_end']
            sample = json.loads(r['sample_json'])
            assert all(d['date'] <= r['as_of'] for d in sample['news'])
            assert all(d['filing_date'] <= r['as_of'] for d in sample['filings'])
        assert all(dates == sorted(dates) for dates in by_case.values())
    assert counts['none'] == 43
    assert counts['none'] <= counts['monthly'] <= len(daily)
    assert counts['none'] <= counts['weekly'] <= len(daily)


def test_window_buckets_include_end_date_and_partial_tail():
    from backend.api.datasets import walk_window
    def observation(day):
        return {'sample_id':'case','window_start':'2026-01-29','window_end':'2026-02-08',
                'as_of':day,'document_ids':[day], 'news_batch':day,'filing_batch':'',
                'sample_json':json.dumps({'news':[{'date':day}], 'filings':[]})}
    data=[observation(d) for d in ('2026-01-29','2026-01-31','2026-02-01','2026-02-08')]
    weekly=walk_window(data,'weekly')
    monthly=walk_window(data,'monthly')
    assert [r['as_of'] for r in weekly]==['2026-02-04','2026-02-08']
    assert [r['as_of'] for r in monthly]==['2026-01-31','2026-02-08']
    assert monthly[0]['document_ids']==['2026-01-29','2026-01-31']
    assert monthly[-1]['document_ids']==['2026-02-01','2026-02-08']
