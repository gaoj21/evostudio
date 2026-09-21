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
def test_preview_reads_only_one_record_even_with_large_batch(preview_client,base):
    client,rid=preview_client
    code=f'''from torch.utils.data import {base}
class Rows({base}):
    def __len__(self): return 1000000000
    def __getitem__(self, i):
        if i >= 1: raise RuntimeError("Read beyond preview budget")
        return {{"amount":i}}
    def __iter__(self):
        for i in range(1000000000): yield self[i]
def build_dataset(resource): return Rows()
'''
    result=client.post('/api/dataloaders/preview',json={'loader':'python','resource_id':rid,'code':code,'read_batch_size':1024,'preview_mode':'sample'})
    assert result.status_code==200,result.text
    value=result.json()
    assert value['sample_count']==1 and value['sample_limit']==1 and value['snapshot'] is None
    assert value['fields'][0]['name']=='amount'
    assert value['fields'][0]['type']=='int'


def test_invalid_declared_types_rejected():
    with pytest.raises(ValueError,match='types'):
        declared_outputs('OUTPUT_SCHEMA = [{"name":"text","type":"wrong"}]')


def test_missing_schema_never_falls_back_to_running_code(preview_client, monkeypatch):
    client,rid=preview_client
    monkeypatch.setattr(dataloaders, 'raw_records', lambda *a,**kw: pytest.fail('Interface inspection executed Dataset'))
    result=client.post('/api/dataloaders/preview',json={'loader':'python','resource_id':rid,'code':'def build_dataset(resource): raise RuntimeError("Never run")'})
    assert result.status_code==422
    assert 'OUTPUT_SCHEMA' in result.json()['detail']


def test_static_interface_needs_no_dataset_or_valid_run_settings(preview_client, monkeypatch):
    client, _ = preview_client
    monkeypatch.setattr(dataloaders, 'raw_records', lambda *a, **kw: pytest.fail('Must not read data'))
    code = '''OUTPUT_SCHEMA = [{"name":"amount","type":"float"}]
def build_dataset(resource, path: str):
    raise RuntimeError("No execution during interface inspection")
'''
    response = client.post('/api/dataloaders/preview', json={
        'loader': 'python', 'code': code, 'read_batch_size': 0, 'batch_timeout': 0,
        'reader_config': {}, 'preview_mode': 'interface'})
    assert response.status_code == 200, response.text
    assert response.json()['fields'] == [{'name':'amount','type':'float','required':True,'nullable':False}]
    sample = client.post('/api/dataloaders/preview', json={
        'loader': 'python', 'code': code, 'preview_mode': 'sample'})
    assert sample.status_code == 422  # actual sampling still needs a resource


def test_static_interface_validates_factory_and_reference_name(preview_client):
    client, _ = preview_client
    config = {'loader':'python','code':'OUTPUT_SCHEMA = [{"name":"a","type":"int"}]'}
    assert client.post('/api/dataloaders/preview', json=config).status_code == 422
    config['code'] += '\ndef build_dataset(resource): return None'
    result = client.post('/api/dataloaders/preview', json={**config,'input_mode':'reference','reference_field':'_dataloader'})
    assert result.status_code == 422


def test_explicit_sampling_can_detect_fields_even_if_declaration_is_invalid(preview_client, monkeypatch):
    client, rid = preview_client
    code = 'OUTPUT_SCHEMA = [{"name":"wrong","type":"invalid"}]\ndef build_dataset(resource): pass'
    monkeypatch.setattr(dataloaders, 'raw_records', lambda *a, **kw: ([{'actual':3}], {}))
    config = {'loader':'python','resource_id':rid,'code':code}
    assert client.post('/api/dataloaders/preview', json=config).status_code == 422
    sampled = client.post('/api/dataloaders/preview', json={**config,'preview_mode':'sample'})
    assert sampled.status_code == 200, sampled.text
    assert sampled.json()['fields'][0]['name'] == 'actual'
