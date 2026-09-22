import importlib.util
import json
import sys
from pathlib import Path

import pytest

HERE=Path(__file__).resolve().parents[2]/'projects/credit_risk/dataset/builders'
sys.path.insert(0,str(HERE))
spec=importlib.util.spec_from_file_location('expansion_builder',HERE/'build_r10_expand.py')
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def test_filing_only_company_has_real_dated_evidence():
    c={'cik':'123','name':'Example Corporation','symbol':'EX','query_name':'Example'}
    filings=[{'filing_date':'2025-01-10','adsh':'a','text':'actual disclosure','items':['2.04']},
             {'filing_date':'2025-02-10','adsh':'b','text':'future disclosure'}]
    docs,_=m.documents(c,'id',[],filings,{'start':'2025-01-01','end':'2025-01-31'})
    assert len(docs)==1
    assert docs[0]['text']=='actual disclosure'


def test_expansion_preserves_corrected_legacy_window():
    candidate={'ref_date':'2025-01-03'}
    prior={'window':{'start':'2024-04-13','end':'2024-10-09'}}
    assert m.cached_window(candidate,prior,None)==prior['window']
    assert m.cached_window(candidate,prior,{'event_date':'2024-10-09'})['end']=='2024-10-08'
    assert m.cached_window(candidate,None,None)['end']=='2025-01-02'


def test_reviewed_backfill_respects_entity_publication_date_and_review_status():
    evidence=[{'cik':'123','published_at':'2025-01-10','source_url':'https://example.org/a',
               'title':'Disclosure','summary':'Known then','review_status':'primary_source_checked'}]
    window={'start':'2025-01-01','end':'2025-01-09'}
    assert m.reviewed_documents(evidence,'123',window)==[]
    window['end']='2025-01-10'
    assert m.reviewed_documents(evidence,'456',window)==[]
    assert m.reviewed_documents(evidence,'123',window)[0]['text']=='Known then'
    evidence[0]['review_status']='search_only'
    with pytest.raises(ValueError):m.reviewed_documents(evidence,'123',window)


def test_extracts_substance_beyond_cover_page():
    text='cover '*700+'Item 2.04 Triggering events. Debt accelerated today.'
    assert 'Debt accelerated today' in m.passages(text)
    assert m.passages(text).startswith('[characters 4200:')


def test_observation_cannot_see_later_date_or_outcome():
    case={'case_id':'opaque','company':{'name':'Example','cik':'123'},
          'window':{'start':'2025-01-01','end':'2025-06-01'},
          'documents':[{'document_id':'a','kind':'news','title':'First','available_at':'2025-01-02','text':'known'},
                       {'document_id':'b','kind':'news','title':'Future','available_at':'2025-05-01','text':'secret'}]}
    o=m.observation(case,'2025-01-02')
    assert o['window_end']=='2025-01-02'
    assert o['document_ids']==['a']
    assert 'secret' not in json.dumps(o)
    assert 'label' not in o and 'event_type' not in o


def test_cached_negatives_do_not_gain_gold_and_company_partitions_stay_together(tmp_path):
    base=tmp_path/'live';base.mkdir()
    (base/'samples.jsonl').write_text('')
    (base/'candidates.csv').write_text('type,cik,name,query_name,symbol,ref_date\nnegative,123,Example Corporation,Example,EX,2025-02-01\npositive,456,Empty Corporation,Empty,EMPTY,2025-02-01\n')
    p=base/'raw/filings';p.mkdir(parents=True)
    (p/'neg_EX.jsonl').write_text(json.dumps({'filing_date':'2025-01-10','form':'8-K','adsh':'abc','text':'financial disclosure'})+'\n')
    curated=tmp_path/'events.json';curated.write_text(json.dumps([{
        'key':'second','cik':'123','name':'Example Corporation','symbol':'EX','sector':'example',
        'event_type':'refinancing','published_at':'2025-01-15','source_url':'https://example.org',
        'summary':'Known financing','review_status':'fixture'}]))
    reviews=tmp_path/'reviews.json';reviews.write_text('{}')
    output=tmp_path/'release'
    manifest=m.build(base,curated,reviews,output)
    assert manifest['company_count']==1 and manifest['case_count']==2
    assert len(manifest['excluded_companies'])==1
    labels=m.read_rows(output/'outcomes.jsonl')
    assert all(not x['gold_eligible'] for x in labels)
    assert labels[0]['event_type']=='unverified' and labels[0]['risk_level'] is None
    assert len({p['partition'] for p in m.read_rows(output/'partitions.jsonl')})==1
    assert len({p['case_id'] for p in m.read_rows(output/'cases.jsonl')})==2
    with pytest.raises(ValueError):m.build(base,curated,reviews,output)
    with pytest.raises(ValueError):m.build(base,curated,reviews,base/'nested')

    audit=tmp_path/'audit.json'
    key=m.digest('cached|123|2025-02-01')
    audit.write_text(json.dumps([{'case_id':key,'review_status':'negative_followup_insufficient',
                                 'remaining_requirements':['define_followup_horizon']}]))
    reviewed=tmp_path/'reviewed'
    m.build(base,curated,reviews,reviewed,audit_path=audit)
    label=next(x for x in m.read_rows(reviewed/'outcomes.jsonl') if x['case_id']==key)
    assert label['event_type']=='unverified' and not label['gold_eligible']
    assert 'define_followup_horizon' in label['review_reasons']
    assert all('outcome_audit' not in o for o in m.read_rows(reviewed/'observations.jsonl'))
