import sys
from pathlib import Path
import hashlib

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'projects/credit_risk/dataset/builders'))
from build_r13_clean import clean_document, isolated_partitions


def test_quarantine_preserves_source_and_input():
    original={'kind':'news','text':'blocked','title':'Company funding','source_url':'https://example.org','representation':'cached_article'}
    digest=hashlib.sha256(b'blocked').hexdigest()
    clean=clean_document(original,{digest:'access restriction'})
    assert original['text']=='blocked'
    assert clean['text']=='' and clean['representation']=='headline_only'
    assert clean['title']==original['title'] and clean['source_url']==original['source_url']
    assert clean['quarantined_body_sha256']==digest
    assert clean_document(dict(original,kind='sec_filing'),{digest:'blocked'})['text']=='blocked'


def test_transitive_shared_evidence_cannot_cross_partitions():
    cases=[{'case_id':'a','group_id':'cik:1','documents':[{'document_id':'1','text':'shared'}]},
           {'case_id':'b','group_id':'cik:2','documents':[{'document_id':'2','text':'shared'},{'document_id':'3','text':''}]},
           {'case_id':'c','group_id':'cik:3','documents':[{'document_id':'3','text':''}]},
           {'case_id':'d','group_id':'cik:4','documents':[{'document_id':'4','text':''}]}]
    prior=[{'case_id':x,'partition':'legacy_review' if x=='a' else 'holdout_candidate'} for x in 'abcd']
    result={r['case_id']:r for r in isolated_partitions(cases,prior)}
    assert {result[x]['group_id'] for x in 'abc'}=={'cik:1'}
    assert all(result[x]['partition']=='legacy_review' for x in 'abc')
    assert result['d']['partition']=='holdout_candidate'
