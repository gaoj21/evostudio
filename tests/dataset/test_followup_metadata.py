import sys
from pathlib import Path
from datetime import date
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'projects/credit_risk/dataset/builders'))
from build_r14_metadata import classify
from build_r15_followup import interval_packet


def test_sic_ranges_do_not_merge_retail_and_manufacturing():
    assert classify('2836')=='manufacturing'
    assert classify('7372')=='services'
    assert classify('5311')=='retail'
    assert classify('0000')=='unclassified'


def test_interval_excludes_outside_dates_and_never_certifies_absence():
    case={'case_id':'a','cik':'123','company':'Example','window':{'end':'2025-01-01'}}
    filings=[{'accessionNumber':str(i),'filingDate':d,'form':'8-K','items':'2.04','primaryDocument':'doc.htm'} for i,d in enumerate(['2025-01-01','2025-01-02','2025-04-01','2025-04-02'])]
    result=interval_packet(case,filings,date(2025,4,2))
    assert [f['filed_at'] for f in result['filings']]==['2025-01-02','2025-04-01']
    assert all(f['priority']=='default_or_insolvency_item' for f in result['filings'])
    assert result['negative_verified'] is False
    assert interval_packet(case,[],date(2025,2,1))['status']=='right_censored'
