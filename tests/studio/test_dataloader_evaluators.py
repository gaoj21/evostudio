import copy
import json
import pytest
from fastapi.testclient import TestClient
from backend.features.data import data_resources, dataloaders
from backend.features.evaluation import evaluator_tools, canvas_evolution
from backend.api import graphs, runner, run_plan, sources, tools_registry, batch


@pytest.fixture
def client(tmp_path, monkeypatch):
    from backend.api.app import app
    monkeypatch.setattr(data_resources, 'data_path', lambda *p: tmp_path.joinpath(*p))
    return TestClient(app)


def upload(client, files):
    result = client.post('/api/data-resources', files=[('files', (name, value)) for name, value in files])
    assert result.status_code == 200, result.text
    return result.json()


def test_folder_upload_nested_json_and_full_preprocessing(client, monkeypatch):
    resource = upload(client, [('folder/a.json', b'{"data":{"items":[{"company":"B","day":"2020-02-01","x":2},{"company":"A","day":"2020-03-01","x":3}]}}'),
                               ('folder/b.json', b'{"data":{"items":[{"company":"A","day":"2020-01-01","x":1}]}}')])
    calls = []
    monkeypatch.setattr(tools_registry, 'call_tool', lambda name, args: calls.append(args) or args['records'])
    cfg = {'type': 'dataloader', 'resource_id': resource['id'], 'record_path': 'data.items',
           'group_by': 'company', 'order_by': 'day', 'read_batch_size': 2, 'transform_tool':'prepare', 'transform_scope':'all'}
    rows, meta = dataloaders.prepare(cfg)
    assert len(calls) == 1 and len(calls[0]['records']) == 3
    assert [r['x'] for r in rows] == [1, 3, 2]
    assert meta['raw_records'] == 3
    assert len({r['_dataloader']['record_id'] for r in rows}) == 3
    cfg['preview_snapshot'] = meta['snapshot']
    assert dataloaders.prepare(cfg)[1]['snapshot'] == meta['snapshot']
    assert [len(chunk) for chunk in dataloaders.iter_batches(cfg)] == [2,1]
    assert batch._group_key(rows[0]) == batch._group_key(rows[1]) != batch._group_key(rows[2])
    mapped = sources.map_to_workflow_inputs(rows, [{'name':'x','required':True}])
    assert mapped[0]['_dataloader'] == rows[0]['_dataloader']
    preview = client.post('/api/dataloaders/preview', json=cfg)
    assert preview.status_code == 422
    assert 'OUTPUT_SCHEMA' in preview.json()['detail']


@pytest.mark.parametrize('name', ['../escape.json', '/tmp/escape', 'folder/../../escape', 'a/./b'])
def test_upload_rejects_unsafe_paths_atomically(client, name):
    result = client.post('/api/data-resources', files=[('files', ('good.txt', b'ok')), ('files', (name, b'bad'))])
    assert result.status_code == 422
    assert client.get('/api/data-resources').json()['resources'] == []


def test_arbitrary_files_and_folder_records(client):
    resource = upload(client, [('company/a.txt', b'hello'), ('company/b.bin', b'\x00\xff')])
    rows = dataloaders.records({'resource_id':resource['id'], 'loader':'folders'})
    assert len(rows) == 1 and rows[0]['folder'] == 'company'
    assert rows[0]['files'][1]['content'] is None
    reference = dataloaders.records({'resource_id':resource['id'], 'loader':'files','input_mode':'reference','reference_field':'documents'})
    assert len(reference[0]['documents']) == 2


def graph_with_evaluator(timing='run', kind='exact_match'):
    return {'id':'loader-evaluation', 'name':'Test', 'goal':'test', 'flow_version':2,
            'tasks':[{'name':'work','kind':'tool','tool':'echo','inputs':[{'name':'text','type':'str','required':True}], 'outputs':[{'name':'answer','type':'str','required':True}]},
                     {'name':'quality','kind':'evaluator','evaluator':{'type':kind,'timing':timing,'label_field':'expected'},
                      'inputs':[{'name':'prediction','type':'any','required':True},{'name':'expected','type':'any','required':False}], 'outputs':[]}],
            'edges':[{'source':'work','target':'quality','mappings':[{'from':'answer','to':'prediction'}]}]}


def mock_tool(monkeypatch):
    monkeypatch.setattr(tools_registry,'find_tool',lambda name: {})
    monkeypatch.setattr(tools_registry,'validate_tool_names',lambda names:None)
    monkeypatch.setattr(tools_registry,'call_tool',lambda name,args,**kwargs: args['text'])


@pytest.mark.parametrize('timing', ['node','run'])
def test_evaluator_preserves_prediction_reads_full_intermediate_and_replays_without_llm(monkeypatch, timing):
    mock_tool(monkeypatch)
    graph = graph_with_evaluator(timing)
    plan = run_plan.compile_plan(graph)
    assert [node['name'] for node in plan['nodes']] == ['work']
    assert [item['name'] for item in plan['inputs']] == ['text']
    content = 'complete text ' * 100
    run_id = runner.start_run(graph, {'text':content, 'expected':content}, background=False)
    result = runner.get_run(run_id)
    assert result['status'] == 'success', result.get('error')
    assert result['result'] == {'answer':content}
    assert result['evaluations']['quality']['metrics']['accuracy'] == 1
    assert result['node_outputs']['work']['answer'] == content
    assert evaluator_tools.evaluate_runs(graph,[result])['quality']['metrics']['accuracy'] == 1
    assert result['execution_snapshot']['graph'] == graph


def test_evaluator_cannot_feed_agents(monkeypatch):
    mock_tool(monkeypatch)
    graph = graph_with_evaluator()
    graph['edges'].append({'source':'quality','target':'work','control_only':True})
    with pytest.raises(graphs.GraphValidationError,match='Evaluator outputs'):
        graphs.validate_graph(graph)


def test_missing_labels_are_unscored_not_negative():
    result = evaluator_tools.report({'type':'exact_match'}, [{'id':'1','prediction':'x','expected':None}, {'id':'2','prediction':'x','expected':'x'}])
    assert result['coverage'] == {'total':2,'scored':1,'unscored':1}
    assert result['metrics']['accuracy'] == 1


def test_custom_tool_receives_all_saved_nodes_and_can_aggregate(monkeypatch):
    graph = graph_with_evaluator('batch', 'tool')
    graph['tasks'][-1]['evaluator'].update(tool='custom', metric='score')
    runs = [{'run_id': 'one', 'inputs': {'folder':'A'}, 'status':'success',
             'result': {'done':True}, 'nodes':[{'name':'unconnected','status':'success'}],
             'node_outputs': {'work':{'answer':'yes'},'unconnected':{'text':'x'*2000}},
             'execution_snapshot': {'version':1}},
            {'run_id':'two','inputs':{'folder':'A'},'status':'failed','error':'boom','node_outputs':{}}]
    original = copy.deepcopy(runs)
    def call(name, args):
        assert name == 'custom'
        rows = args['records']
        assert len(rows) == 2 and rows[1]['error'] == 'boom'
        assert rows[0]['node_outputs']['unconnected']['text'] == 'x'*2000
        assert rows[0]['nodes'][0]['status'] == 'success'
        assert rows[0]['result'] == {'done':True}
        assert rows[0]['execution_snapshot'] == {'version':1}
        assert rows[0]['focus'][0]['source'] == 'work'
        rows[0]['inputs']['folder'] = 'mutated'
        return {'metrics':{'score':0.5}, 'records':[{'id':'folder-A','score':0.5}],
                'coverage':{'unit':'folders','total':1,'scored':1,'unscored':0}}
    monkeypatch.setattr(tools_registry, 'call_tool', call)
    result = evaluator_tools.evaluate_runs(graph, runs)['quality']
    assert result['status'] == 'success'
    assert result['coverage']['total'] == 1
    assert runs == original


def test_separate_labels_are_passed_unjoined_to_custom_tool(client, monkeypatch):
    resource = upload(client,[('labels.json',b'[{"key":"A","expected":1},{"key":"A","expected":2}]')])
    graph = graph_with_evaluator('batch','tool')
    graph['tasks'][-1]['evaluator'].update(tool='custom',labels={'resource_id':resource['id']})
    def call(name, args):
        assert len(args['config']['label_records']) == 2
        assert args['records'][0]['inputs'] == {'folder':'A'}
        return {'metrics':{'score':1},'details':{'matched':True}}
    monkeypatch.setattr(tools_registry,'call_tool',call)
    result = evaluator_tools.evaluate_runs(graph,[{'inputs':{'folder':'A'},'status':'success'}])['quality']
    assert result['status'] == 'success'
    assert 'coverage' not in result


def test_custom_evaluator_allows_metrics_without_record_scores(monkeypatch):
    monkeypatch.setattr(tools_registry,'call_tool',lambda *a,**k: {'metrics':{'score':1}})
    result = evaluator_tools.report({'type':'tool','tool':'custom'}, [{'id':'1','prediction':'answer'}])
    assert result['metrics']['score'] == 1
    assert 'coverage' not in result


@pytest.mark.parametrize('coverage', [
    {'total':1,'scored':1,'unscored':0},
    {'unit':'documents','total':1,'scored':2,'unscored':0},
])
def test_custom_evaluator_validates_declared_coverage(monkeypatch, coverage):
    monkeypatch.setattr(tools_registry,'call_tool',lambda *a,**k: {'metrics':{'score':1},'coverage':coverage})
    with pytest.raises(sources.SourceError):
        evaluator_tools.report({'type':'tool','tool':'custom'}, [])


def test_canvas_evolution_validates_candidates_and_keeps_best(monkeypatch):
    from backend.features.evaluation import saved_result_evolution as saved
    graph = graph_with_evaluator()
    graph['tasks'][0].update(kind='task',prompt='old')
    observed = []
    def start(candidate,row,**kwargs):
        observed.append(copy.deepcopy(candidate))
        return 'good' if candidate['tasks'][0]['prompt']=='new' else 'bad'
    monkeypatch.setattr(runner,'start_run',start)
    monkeypatch.setattr(runner,'get_run',lambda id: {'status':'success','node_outputs':{'work':{'answer': 'yes' if id=='good' else 'no'}},'nodes':[]})
    monkeypatch.setattr(runner,'_make_llm',lambda: object())
    def propose(candidate,records,chosen,llm,feedback=None):
        assert feedback['metrics']['score']==0
        result=copy.deepcopy(candidate);result['tasks'][0]['prompt']='new'
        return result,[],1
    monkeypatch.setattr(saved,'propose',propose)
    state={'task_id':'experiment','execution_graph':copy.deepcopy(graph)}
    canvas_evolution.execute(state,graph,[{'text':'question','expected':'yes'}],{'evaluator':'quality','mode':'evolve_evaluate','nodes':['work'],'rounds':1},lambda text: None)
    assert state['baseline']['metrics']['score']==0
    assert state['optimized']['metrics']['score']==1
    assert state['validation_status']=='evaluated'
    assert observed[0]['id'] != observed[1]['id']
    assert state['optimized_graph']['id']==graph['id']


def test_registered_tool_dispatch_uses_component_contract(client):
    resource = upload(client,[('x.json',b'[{"value":1},{"value":2}]')])
    result = tools_registry.call_tool('read_dataset', {'config':{'resource_id':resource['id']}})
    assert len(result['records']) == 2
    scored = tools_registry.call_tool('evaluate_records',{'config':{'type':'exact_match'}, 'records':[{'id':'1','prediction':'a','expected':'a'}]})
    assert scored['metrics']['accuracy'] == 1


def test_resource_delete_blocked_by_label_reference(client):
    resource = upload(client,[('labels.json',b'[{"id":"A","expected":1}]')])
    graph=graph_with_evaluator()
    graph['tasks'][-1]['evaluator']['labels']={'resource_id':resource['id']}
    graphs.GRAPHS_DIR.mkdir(parents=True,exist_ok=True)
    (graphs.GRAPHS_DIR/'example.json').write_text(json.dumps(graph))
    assert client.delete('/api/data-resources/'+resource['id']).status_code == 409


def test_loader_cache_reuses_transform_but_invalidates_on_tool_change(client,monkeypatch):
    from backend.api import custom_tools
    resource = upload(client,[('x.json',b'[{"value":1}]')])
    calls=[]
    monkeypatch.setattr(custom_tools,'find',lambda name: {'code':'version one'})
    monkeypatch.setattr(tools_registry,'call_tool',lambda name,args: calls.append(1) or args['records'])
    cfg={'resource_id':resource['id'],'transform_scope':'all','transform_tool':'clean'}
    dataloaders.records(cfg);dataloaders.records(cfg)
    assert len(calls)==1
    monkeypatch.setattr(custom_tools,'find',lambda name: {'code':'version two'})
    dataloaders.records(cfg)
    assert len(calls)==2


def test_loader_trajectory_batch_grouping_works_without_risk_field_names():
    rows=[({'id':i},{'_dataloader':{'group':group,'order':i}}) for i,group in enumerate(['A','B','A'])]
    groups=batch._grouped(rows)
    assert [[item['id'] for item,_ in group] for group in groups] == [[0,2],[1]]


def test_saved_evolve_uses_canvas_evaluator_instead_of_legacy_metric(client,monkeypatch):
    from backend.api import evolve_api
    graph=graph_with_evaluator()
    graphs.GRAPHS_DIR.mkdir(parents=True,exist_ok=True)
    (graphs.GRAPHS_DIR/'loader-evaluation.json').write_text(json.dumps(graph))
    monkeypatch.setattr(runner,'get_run',lambda id: {'run_id':id,'graph_id':graph['id'],'status':'success','inputs':{'expected':'yes'},'node_outputs':{'work':{'answer':'yes'}},'nodes':[]})
    captured=[]
    monkeypatch.setattr(evolve_api,'start_evolve',lambda *args: captured.append(args) or 'evolve-test')
    response=client.post('/api/graphs/loader-evaluation/evolve',json={'source':'saved_run','run_id':'run-test','evaluator':'quality','mode':'evaluate'})
    assert response.status_code==200,response.text
    assert captured[0][2]=='canvas:quality'
    assert captured[0][3]['evaluator']=='quality'
    scored=canvas_evolution.score_saved(graph,captured[0][1],'quality')
    assert scored['metrics']['score']==1


def test_shared_reference_is_available_to_loader_preprocessor(client,monkeypatch):
    from backend.features.data import input_composition
    resource=upload(client,[('news.json',b'[{"company":"A"},{"company":"B"}]')])
    labels=upload(client,[('companies.json',b'[{"name":"A"}]')])
    primary={'name':'news','kind':'source','source':{'type':'dataloader','resource_id':resource['id'],'transform_scope':'all','transform_tool':'match'},'outputs':[{'name':'company'}]}
    reference={'name':'companies','kind':'source','source':{'type':'dataloader','resource_id':labels['id'],'input_mode':'reference','reference_field':'obligors'},'outputs':[{'name':'obligors'}]}
    graph={'tasks':[primary,reference], 'edges':[{'source':'news','target':'work'},{'source':'companies','target':'work'}]}
    monkeypatch.setattr(tools_registry,'call_tool',lambda name,args:[row for row in args['records'] if row['company'] in {r['name'] for r in row['obligors']}])
    rows=input_composition.load_primary(graph,primary)
    assert [row['company'] for row in rows]==['A']


@pytest.mark.parametrize('python_reader', [False, True])
def test_single_run_loader_retains_actual_inputs_for_saved_evaluation(client,monkeypatch,python_reader):
    mock_tool(monkeypatch)
    resource=upload(client,[('data.json',b'[{"text":"yes","expected":"yes"}]')])
    graph=graph_with_evaluator()
    graph['tasks'].insert(0,{'name':'input','kind':'source','source':{'type':'dataloader','resource_id':resource['id']},'outputs':[{'name':'text','type':'str'},{'name':'expected','type':'str'}]})
    graph['edges'].insert(0,{'source':'input','target':'work','mappings':[{'from':'text','to':'text'}]})
    if python_reader:
        graph['tasks'][0]['source'].update(loader='python', code=PYTORCH_CODE, reader_config={'multiplier':2})
    id=runner.start_run(graph,{},background=False)
    result=runner.get_run(id)
    assert result['status']=='success',result.get('error')
    assert result['inputs']['expected']=='yes'
    assert result['evaluations']['quality']['metrics']['accuracy']==1
    assert result['nodes'][-1]['status']=='completed'
    assert evaluator_tools.evaluate_runs(graph,[result])['quality']['metrics']['accuracy']==1


def test_csv_limit_survives_other_library_imports():
    import csv
    from backend.features.data.user_datasets import parse
    csv.field_size_limit(5)
    rows,_=parse('long.csv',b'body\nmore than five characters\n')
    assert rows[0]['body']=='more than five characters'


def test_api_dataloader_batch_and_canvas_evaluator_end_to_end(client,monkeypatch):
    mock_tool(monkeypatch)
    resource=upload(client,[('records.json',b'[{"text":"one","expected":"one"},{"text":"two","expected":"different"},{"text":"three"}]')])
    created=client.post('/api/graphs',json={'name':'DataLoader demo','goal':'Echo and evaluate'}).json()
    graph=graph_with_evaluator('batch')
    graph.update(id=created['id'],name=created['name'])
    graph['tasks'].insert(0,{'name':'input','kind':'source','source':{'type':'dataloader','resource_id':resource['id'],'read_batch_size':2},'outputs':[{'name':'text','type':'str','required':True}]})
    graph['edges'].insert(0,{'source':'input','target':'work','mappings':[{'from':'text','to':'text'}]})
    # expected is evaluator-only; it need not be declared as a model input.
    graph['tasks'][0]['outputs'].append({'name':'expected','type':'str','required':False})
    saved=client.put('/api/graphs/'+graph['id'],json=graph)
    assert saved.status_code==200,saved.text
    assert not saved.json().get('project_error'),saved.json().get('project_error')
    preview=client.post('/api/graphs/'+graph['id']+'/run-batch/preview',json={'source':'canvas'})
    assert preview.status_code==200,preview.text
    assert preview.json()['total'] is None
    assert preview.json()['deferred'] is True
    started=client.post('/api/graphs/'+graph['id']+'/run-batch',json={'source':'canvas'})
    assert started.status_code==200,started.text
    batch_id=started.json()['batch_id']
    assert batch.wait_for(batch_id,timeout=10)
    result=batch.get_batch(batch_id)
    assert result['status']=='succeeded',result
    evaluation=result['evaluations']['quality']
    assert evaluation['coverage']=={'total':3,'scored':2,'unscored':1}
    assert evaluation['metrics']['accuracy']==0.5
    report=client.post('/api/evaluators/graphs/'+graph['id']+'/saved',json={'batch_id':batch_id})
    assert report.status_code==200,report.text
    assert report.json()['evaluations']['quality']['metrics']['accuracy']==0.5


def test_custom_evaluator_run_receives_final_result_without_private_runtime(monkeypatch):
    graph = graph_with_evaluator('run', 'tool')
    graph['tasks'][-1]['evaluator'].update(tool='custom')
    monkeypatch.setattr(tools_registry,'find_tool',lambda name: {})
    monkeypatch.setattr(tools_registry,'validate_tool_names',lambda names:None)
    def call(name,args,**kwargs):
        if name != 'custom':
            return args['text']
        record = args['records'][0]
        assert record['result'] == {'answer':'hello'}
        assert record['node_outputs']['work'] == {'answer':'hello'}
        assert not any(k.startswith('_') for k in record)
        return {'metrics':{'score':1}}
    monkeypatch.setattr(tools_registry,'call_tool',call)
    run_id = runner.start_run(graph, {'text':'hello'}, background=False)
    run = runner.get_run(run_id)
    assert run['evaluations']['quality']['status'] == 'success'
    scored = canvas_evolution.score_saved(graph, [run], 'quality')
    assert scored['metrics'] == {'score':1}
    assert scored['records'] == {}


PYTORCH_CODE = '''import json
from pathlib import Path
from torch.utils.data import Dataset
class Records(Dataset):
    def __init__(self, records): self.records = records
    def __len__(self): return len(self.records)
    def __getitem__(self, index): return self.records[index]
def build_dataset(resource, config):
    rows = json.loads((Path(resource['root']) / resource['files'][0]['path']).read_text())
    return Records([dict(row, multiplier=config['multiplier']) for row in rows])
'''


def test_pytorch_code_preview_and_batch_use_same_records_and_keep_tail(client):
    resource = upload(client,[('data.json',b'[{"value":1},{"value":null},{"value":3}]')])
    config = {'type':'dataloader','loader':'python','resource_id':resource['id'],'code':PYTORCH_CODE,
              'reader_config':{'multiplier':2},'read_batch_size':2}
    response = client.post('/api/dataloaders/preview',json={**config,'preview_mode':'sample'})
    assert response.status_code == 200, response.text
    assert response.json()['engine'] == 'torch.utils.data.DataLoader'
    chunks = list(dataloaders.iter_batches(config))
    assert [len(chunk) for chunk in chunks] == [2,1]
    assert chunks[0][1]['value'] is None
    assert chunks[1][0]['multiplier'] == 2
    assert [{k:v for k,v in row.items() if k != '_dataloader'} for row in dataloaders.records(config)] == response.json()['preview']
    assert response.json()['snapshot'] is None
    assert response.json()['preview_mode'] == 'sample'
    changed = {**config,'code':PYTORCH_CODE.replace("config['multiplier']", "config['multiplier'] + 1")}
    assert dataloaders.records(changed)[0]['multiplier'] == 3


def test_pytorch_iterable_dataset_preserves_partial_batch(client):
    resource=upload(client,[('context.json',b'{}')])
    code='''from torch.utils.data import IterableDataset
class Stream(IterableDataset):
    def __iter__(self):
        for n in range(3): yield {'value': n}
def build_dataset(resource, config): return Stream()
'''
    config={'loader':'python','resource_id':resource['id'],'code':code,'read_batch_size':2}
    assert [len(chunk) for chunk in dataloaders.iter_batches(config)] == [2,1]


@pytest.mark.parametrize('code, message', [
    ('def build_dataset(resource, config): return []', 'must return'),
    ('def build_dataset(:', 'syntax error'),
    ('def other(): return []', 'Define build_dataset'),
    ('def build_dataset(resource, config): raise ValueError("bad dataset")', 'bad dataset'),
])
def test_python_loader_reports_actionable_errors(client, code, message):
    resource=upload(client,[('context.json',b'{}')])
    response=client.post('/api/dataloaders/preview',json={'loader':'python','code':code,'resource_id':resource['id'],'preview_mode':'sample'})
    assert response.status_code == 422
    assert message in response.json()['detail']


def test_all_builtin_records_use_isolated_pytorch_dataloader(client, monkeypatch):
    from backend.features.chat import chat_control
    resource=upload(client,[('records.json',b'[{"a":1},{"a":2},{"a":3}]')])
    calls=[]
    original=chat_control.worker
    def capture(kind, payload, **kwargs):
        calls.append((kind, payload['batch_size']))
        return original(kind, payload, **kwargs)
    monkeypatch.setattr(chat_control,'worker',capture)
    config={'resource_id':resource['id'],'read_batch_size':2}
    assert len(dataloaders.prepare(config)[0]) == 3
    assert not calls  # Preview must not initialize a second native runtime.
    # Consecutive batches, final partial batch kept; cut in-process now, so
    # no subprocess receives a full copy of the records just to slice them.
    assert [len(c) for c in dataloaders.iter_batches(config)] == [2,1]
    assert [r['a'] for c in dataloaders.iter_batches(config) for r in c] == [1, 2, 3]
    assert calls == []


def test_uploaded_dataset_is_visible_in_workspace_before_graph_save(client):
    from pathlib import Path
    from backend.api import workspace
    graph=client.post('/api/graphs',json={'name':'Uploads','goal':'Read inputs'}).json()
    other=client.post('/api/graphs',json={'name':'Other','goal':'Other task'}).json()
    response=client.post('/api/data-resources',data={'graph_id':graph['id']},files=[('files',('folder/dev/a.json',b'[{"text":"yes"}]'))])
    assert response.status_code==200,response.text
    resource=response.json()
    assert Path(resource['root']).is_dir()
    listing=client.get(f"/api/graphs/{graph['id']}/workspace").json()['files']
    entry=next(e for e in listing if e['path'].endswith('folder/dev/a.json'))
    assert Path(entry['absolute_path']).read_text()=='[{"text":"yes"}]'
    read=client.get(f"/api/graphs/{graph['id']}/workspace/file",params={'path':entry['path']})
    assert read.status_code==200
    assert read.json()['readonly'] is True
    assert read.json()['absolute_path']==entry['absolute_path']
    assert client.get(f"/api/graphs/{other['id']}/workspace/file",params={'path':entry['path']}).status_code==404
    for operation in (lambda:workspace.write_file(graph['id'],entry['path'],b'changed'),
                      lambda:workspace.delete_file(graph['id'],entry['path']),
                      lambda:workspace.make_dir(graph['id'],entry['path']+'/child')):
        with pytest.raises(workspace.WorkspaceError,match='immutable'): operation()
    assert client.get(f"/api/graphs/{graph['id']}/workspace/download",params={'path':entry['path']}).content==b'[{"text":"yes"}]'


def test_dataset_example_uses_copied_paths_and_split_and_does_not_cache_external_changes(client,tmp_path):
    from pathlib import Path
    editor=Path('frontend/src/features/data/PythonDatasetEditor.jsx').read_text()
    code=editor.split('export const DATASET_EXAMPLE = `',1)[1].split('`;',1)[0]
    resource=upload(client,[('dev/data.json',b'[{"text":"dev"}]'),('test/data.json',b'[{"text":"test"}]')])
    config={'loader':'python','resource_id':resource['id'],'code':code,'reader_config':{'path':resource['root'],'split':'test'}}
    assert dataloaders.records(config)[0]['text']=='test'
    file=tmp_path/'external.json'
    file.write_text('[{"text":"before","split":"dev"},{"text":"ignored","split":"test"}]')
    config['reader_config']={'path':str(file),'split':'dev'}
    assert dataloaders.records(config)[0]['text']=='before'
    file.write_text('[{"text":"after","split":"dev"}]')
    assert dataloaders.records(config)[0]['text']=='after'


def test_historical_resource_mounts_without_saving_graph_and_preserves_owner(client):
    owner=client.post('/api/graphs',json={'name':'Original upload','goal':'Read'}).json()
    target=client.post('/api/graphs',json={'name':'Reuse upload','goal':'Read'}).json()
    resource=client.post('/api/data-resources',data={'graph_id':owner['id']},files=[('files',('old/data.json',b'[{"x":1}]'))]).json()
    path=f"/api/data-resources/{resource['id']}/workspace"
    for _ in range(2):
        response=client.post(path,json={'graph_id':target['id']})
        assert response.status_code==200,response.text
    assert response.json()['workspace_graph_ids']==[target['id']]
    assert response.json()['graph_id']==owner['id']
    for graph in (owner,target):
        listing=client.get(f"/api/graphs/{graph['id']}/workspace").json()['files']
        entry=next(e for e in listing if e['path'].endswith('old/data.json'))
        assert entry['absolute_path']==resource['root']+'/old/data.json'
        assert entry['readonly'] is True
    assert client.post(path,json={'graph_id':'does-not-exist'}).status_code==404
