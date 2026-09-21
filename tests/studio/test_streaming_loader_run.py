import json
import time
import pytest
from fastapi.testclient import TestClient
from backend.api import graphs, runner, batch, custom_tools
from backend.features.data import data_resources
from backend.features.execution.loader_run import start

CODE='''from torch.utils.data import IterableDataset
class Rows(IterableDataset):
    def __iter__(self):
        for i in range(1000000000):
            if i >= 5: raise RuntimeError("Read past sample count")
            yield {"value": i}
def build_dataset(resource): return Rows()
'''

@pytest.fixture
def setup(tmp_path,monkeypatch):
    from backend.api.app import app
    monkeypatch.setattr(data_resources,'data_path',lambda *p:tmp_path.joinpath(*p))
    monkeypatch.setattr(custom_tools,'TOOLS_DIR',tmp_path/'tools')
    monkeypatch.setattr(batch,'BATCHES_DIR',tmp_path/'batches')
    client=TestClient(app)
    resource=client.post('/api/data-resources',files=[('files',('context.txt',b'x'))]).json()
    spec=custom_tools.validate_spec({'name':'double_stream','code':'def double_stream(value: int) -> int:\n    """Double the value."""\n    return value*2'},[])
    custom_tools.save_custom_tool(spec)
    graph={'id':'streaming-regression','name':'Streaming','goal':'Double','flow_version':2,'tasks':[
        {'name':'input','kind':'source','source':{'type':'dataloader','loader':'python','resource_id':resource['id'],'code':CODE,'read_batch_size':2,'n':5},'inputs':[],'outputs':[{'name':'value','type':'int','required':True}]},
        {'name':'double','kind':'tool','tool':'double_stream','inputs':[{'name':'value','type':'int','required':True}],'outputs':[{'name':'result','type':'int'}]}],
        'edges':[{'source':'input','target':'double','mappings':[{'from':'value','to':'value'}]}]}
    return client,graph


def test_streamed_run_limits_reader_keeps_tail_and_archives_inputs(setup):
    _,graph=setup
    result=start(graph,graph['tasks'][0],{'workers':1})
    assert result['streaming'] and result['total'] is None
    assert batch.wait_for(result['batch_id'],timeout=20)
    state=batch.get_batch(result['batch_id'])
    assert state['status']=='succeeded',state
    assert state['collection_complete'] and state['total']==5
    assert [runner.get_run(i['run_id'])['result']['result'] for i in state['items']]==[0,2,4,6,8]
    assert all(i['inputs_archived'] for i in state['items'])
    assert json.loads((batch.BATCHES_DIR/state['items'][4]['input_file']).read_text())['value']==4


def test_opening_run_does_not_load_dataset(setup,monkeypatch):
    client,graph=setup
    monkeypatch.setattr(graphs,'load_graph',lambda _:graph)
    from backend.features.data import input_composition
    monkeypatch.setattr(input_composition,'load_primary',lambda *a:pytest.fail('Opening Run loaded data'))
    response=client.post('/api/graphs/streaming-regression/run-batch/preview',json={'source':'canvas'})
    assert response.status_code==200,response.text
    assert response.json()['deferred'] and response.json()['record_limit']==5


def test_date_grouping_stays_bounded_and_preserves_tail():
    from backend.features.execution.loader_run import period_chunks
    rows=[{'as_of':'2026-01-01','n':i} for i in range(3)]+[{'as_of':'2026-01-02','n':3}]
    assert [len(c) for c in period_chunks(iter([rows[:2],rows[2:]]),2,'daily','as_of')]==[2,1,1]
    with pytest.raises(Exception,match='chronological'):
        list(period_chunks(iter([[rows[-1],rows[0]]]),2,'daily','as_of'))


def test_stop_while_reader_initializes_terminates_and_does_not_claim_completion(setup):
    _,graph=setup
    graph['tasks'][0]['source']['code']='''import time
from torch.utils.data import Dataset
def build_dataset(resource):
    time.sleep(60)
    return Dataset()
'''
    result=start(graph,graph['tasks'][0],{'workers':1})
    time.sleep(.15)
    batch.cancel_batch(result['batch_id'])
    assert batch.wait_for(result['batch_id'],timeout=5)
    state=batch.get_batch(result['batch_id'])
    assert state['status']=='cancelled'
    assert not state['collection_complete']


def test_json_reader_streams_arrays_and_rejects_trailing_data(tmp_path):
    from backend.features.data.json_stream import read_json_records
    path=tmp_path/'data.json';path.write_text('[{"text":"Unicode 世界"},{"n":2}]')
    assert list(read_json_records(path,chunk_size=2))==[{'text':'Unicode 世界'},{'n':2}]
    path.write_text('[{"n":1}] garbage')
    with pytest.raises(ValueError,match='Extra data'): list(read_json_records(path,chunk_size=2))


def test_reader_waits_for_chunk_consumption_instead_of_prefetching_all(setup,tmp_path):
    _,graph=setup
    from backend.features.data.dataset_stream import chunks
    marker=tmp_path/'read-count'
    code='''from pathlib import Path
from torch.utils.data import IterableDataset
class Rows(IterableDataset):
    def __iter__(self):
        for i in range(20):
            Path(MARKER).write_text(str(i+1))
            yield {"value": i}
def build_dataset(resource): return Rows()
'''.replace('MARKER',repr(str(marker)))
    config={**graph['tasks'][0]['source'],'code':code,'n':0}
    stream=chunks(config)
    try:
        assert next(stream)==[{'value':0},{'value':1}]
        time.sleep(.2)
        assert marker.read_text()=='2'
        assert next(stream)==[{'value':2},{'value':3}]
    finally: stream.close()


def test_dataset_length_is_reported_before_first_record_and_respects_selection(setup):
    _, graph = setup
    graph['tasks'][0]['source'].update(code='''import time
from torch.utils.data import Dataset
class Rows(Dataset):
    def __len__(self): return 17
    def __getitem__(self, i):
        time.sleep(60)
        return {"value": i}
def build_dataset(resource): return Rows()
''', offset=2, n=4)
    result = start(graph, graph['tasks'][0], {'workers':1})
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = batch.get_batch(result['batch_id'])
            if state.get('dataset_initialized'): break
            time.sleep(.05)
        assert state.get('dataset_length') == 17, state
        assert state.get('input_total') == 4
        assert state['items'] == []  # no record or batch needed to get the count
        assert batch._digest(state)['input_total'] == 4
    finally:
        batch.cancel_batch(result['batch_id'])
        assert batch.wait_for(result['batch_id'], timeout=5)


def test_unsized_dataset_reports_unknown_not_zero(setup):
    _, graph = setup
    from backend.features.data.dataset_stream import chunks
    info = []
    stream = chunks(graph['tasks'][0]['source'], on_info=info.append)
    try:
        assert next(stream) == [{'value':0},{'value':1}]
        assert info == [{'dataset_length':None, 'selected_records':None}]
    finally:
        stream.close()
