import importlib.util
import json
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[2] / 'projects/credit_risk/dataset/builders/build_r09_review.py'
spec = importlib.util.spec_from_file_location('review_builder', MODULE)
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def sample(cik='1731289', name='Nikola Corp', symbol='NKLA'):
    return {'sample_id':'pos_test', 'type':'positive','split':'dev',
        'company':{'cik':cik,'name':name,'symbol':symbol,'query_name':name},
        'window':{'start':'2024-08-23','end':'2025-02-18'},
        'label':{'event':'bankruptcy','event_date':'2025-02-19'},'news':[], 'filings':[]}


def test_sports_collision_not_counted_as_company():
    assert r.annotate(sample(), {'title':'Nikola Jokic leads Nuggets in NBA'})['entity_status']=='off_target'
    assert r.annotate(sample(), {'title':'Nikola Corp files for bankruptcy'})['entity_status']=='candidate_match'


def test_short_ticker_is_not_a_word_match():
    s=sample('1804591','23andMe Holding Co.','ME')
    assert r.annotate(s,{'title':'Tell ME about a new restaurant'})['entity_status']=='needs_review'
    assert r.annotate(s,{'title':'NASDAQ: ME announces earnings'})['entity_status']=='candidate_match'


def test_gaucho_restaurant_stays_pending():
    s=sample('1559998','Gaucho Group Holdings','VINOQ')
    assert r.annotate(s,{'title':'Gaucho Covent Garden restaurant review'})['entity_status']=='needs_review'


def test_title_cluster_survives_punctuation():
    s=sample()
    a=r.annotate(s,{'title':'Nikola Corp: Earnings Results!'})
    b=r.annotate(s,{'title':'Nikola Corp — earnings results'})
    assert a['title_cluster_id']==b['title_cluster_id']
    assert a['credit_relevance']=='unreviewed'


def test_build_keeps_raw_and_labels_separate_and_marks_gaps(tmp_path):
    s=sample()
    s['news']=[{'title':'Nikola Corp earnings','date':d,'news_id':d,'text':'text '+d}
               for d in ['2025-01-01','2025-01-02','2025-02-18']]
    source=tmp_path/'samples.jsonl';source.write_text(json.dumps(s)+'\n')
    before=source.read_bytes()
    reviews=tmp_path/'reviews.json';reviews.write_text(json.dumps({'1731289':{
        'event_date':'2025-02-17','scope':'registrant','source':'fixture'}}))
    out=tmp_path/'release';m=r.build(source,out,reviews)
    assert source.read_bytes()==before
    assert m['corrected_dates']==1
    annotated=r.load(out/'annotated_samples.jsonl')[0]
    assert len(annotated['news'])==3
    assert annotated['audit']['unfetched_intervals']
    model=r.load(out/'candidate_clean.jsonl')[0]
    assert 'label' not in model and 'type' not in model
    assert len(model['news'])==1
    assert model['news'][0]['date']=='2025-01-01'
    assert 'review' not in model['news'][0]
    assert 'sample_id' not in model
    assert model['input_id']==r.load(out/'outcomes.jsonl')[0]['input_id']
    assert not r.load(out/'outcomes.jsonl')[0]['evaluation_eligible']
    with pytest.raises(ValueError):r.build(source,out,reviews)


def test_unknown_negative_never_becomes_verified_low_risk(tmp_path):
    s=sample();s.update(type='negative');s['label']={'event':None,'event_date':None}
    source=tmp_path/'s.jsonl';source.write_text(json.dumps(s)+'\n')
    reviews=tmp_path/'r.json';reviews.write_text('{}')
    r.build(source,tmp_path/'out',reviews)
    outcome=r.load(tmp_path/'out/outcomes.jsonl')[0]
    assert outcome['status']=='pending'
    assert 'negative_followup_unverified' in outcome['blocking_reasons']
    assert outcome['point_in_time_risk']=='unlabelled'
