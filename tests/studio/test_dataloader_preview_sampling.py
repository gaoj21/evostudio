import pytest
from fastapi.testclient import TestClient
from backend.features.data import data_resources, dataloaders
from backend.features.data.dataset_interface import declared_outputs
from backend.api.app import app

@pytest.fixture
def preview_client(tmp_path, monkeypatch):
    monkeypatch.setattr(data_resources, 'data_path', lambda *p: tmp_path.joinpath(*p))
    monkeypatch.setattr(dataloaders, 'prepare', lambda *_: pytest.fail('Preview must not prepare all records'))
    client=TestClient(app)
    resource=client.post('/api/data-resources',files=[('files',('unused.txt',b'context'))]).json()
    return client, resource['id']


def test_declared_schema_never_executes_module_or_dataset(preview_client):
    client,rid=preview_client
    code='''OUTPUT_SCHEMA = [{"name":"text","type":"str"}]
raise RuntimeError("Never execute this")
def build_dataset(resource):
    raise RuntimeError("Never load this")
'''
    result=client.post('/api/dataloaders/preview',json={'loader':'python','resource_id':rid,'code':code})
    assert result.status_code==200,result.text
    assert result.json()['preview_mode']=='declared'
    assert result.json()['sample_count']==0
    assert result.json()['fields'][0]['type']=='str'


@pytest.mark.parametrize('base', ['Dataset','IterableDataset'])
def test_preview_reads_only_five_records_even_with_large_batch(preview_client,base):
    client,rid=preview_client
    code=f'''from torch.utils.data import {base}
class Rows({base}):
    def __len__(self): return 1000000000
    def __getitem__(self, i):
        if i >= 5: raise RuntimeError("Read beyond preview budget")
        return {{"amount":i}}
    def __iter__(self):
        for i in range(1000000000): yield self[i]
def build_dataset(resource): return Rows()
'''
    result=client.post('/api/dataloaders/preview',json={'loader':'python','resource_id':rid,'code':code,'read_batch_size':1024})
    assert result.status_code==200,result.text
    value=result.json()
    assert value['sample_count']==5 and value['snapshot'] is None
    assert value['fields'][0]['name']=='amount'
    assert value['fields'][0]['type']=='int'


def test_invalid_declared_types_rejected():
    with pytest.raises(ValueError,match='types'):
        declared_outputs('OUTPUT_SCHEMA = [{"name":"text","type":"wrong"}]')
