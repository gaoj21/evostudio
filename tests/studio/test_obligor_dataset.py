"""Expanded datasets can supply their own entity roster without changing defaults."""
from backend.api import obligor_tool


def test_switching_dataset_changes_registry(monkeypatch, tmp_path):
    first=tmp_path/'first.csv';second=tmp_path/'second.csv'
    first.write_text('cik,name,query_name,symbol\n1,First Corp,First,FIRST\n')
    second.write_text('cik,name,query_name,symbol\n2,Second Corp,Second,SECOND\n')
    monkeypatch.setattr(obligor_tool,'_registry',None)
    monkeypatch.setattr(obligor_tool,'_registry_path',None)
    monkeypatch.setattr(obligor_tool,'CANDIDATES_CSV',first)
    monkeypatch.delenv('EAX_STUDIO_OBLIGORS_FILE',raising=False)
    assert obligor_tool._get_registry().match_cik('1') is not None
    monkeypatch.setenv('EAX_STUDIO_OBLIGORS_FILE',str(second))
    assert obligor_tool._get_registry().match_cik('2') is not None
    assert obligor_tool._get_registry().match_cik('1') is None
    monkeypatch.delenv('EAX_STUDIO_OBLIGORS_FILE')
    assert obligor_tool._get_registry().match_cik('1') is not None
