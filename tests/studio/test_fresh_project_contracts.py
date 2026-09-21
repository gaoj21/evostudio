import asyncio
import io
import json
import uuid
import pytest
from fastapi import UploadFile, HTTPException
from backend.features.workspace.project_import import import_graph
from backend.features.data import user_datasets as datasets
from backend.api import graphs, saved_result_evolution as saved
from conftest import make_task, make_graph


def dataset(rows):
    item = {'id':uuid.uuid4().hex, 'name':'Customers', 'fields':list(rows[0]), 'records':rows,
            'created_at':'now', 'row_count':len(rows)}
    datasets.save(item)
    return item


def test_import_preserves_explicit_wiring_and_graph_configuration():
    graph = make_graph([make_task('a', outputs=['value']), make_task('b', inputs=['value'], outputs=['result'])],
                       flow_version=2, preprocess='missing_cleaner',
                       memory_resources=[{'space_id':'a'*32,'name':'Notes'}], memory_positions={'mem:a':{'x':10,'y':20}})
    graph['edges'] = [{'source':'a','target':'b','control_only':True}]
    result = asyncio.run(import_graph(UploadFile(filename='graph.json', file=io.BytesIO(json.dumps(graph).encode()))))
    assert result['flow_version'] == 2
    assert result['edges'] == graph['edges']
    for key in ('preprocess','memory_resources','memory_positions'):
        assert result[key] == graph[key]
    assert any('Preprocessor' in item for item in result['missing_dependencies'])
    assert any('Memory' in item for item in result['missing_dependencies'])


def test_shared_dataset_cannot_be_deleted_until_all_saved_inputs_detached():
    item = dataset([{'amount':1}])
    graphs.GRAPHS_DIR.mkdir(parents=True, exist_ok=True)
    graph = {'id':'consumer','name':'Order project','tasks':[{'name':'feed','source':{'type':'user_dataset','dataset_id':item['id']}}]}
    path = graphs.GRAPHS_DIR/'consumer.json'
    path.write_text(json.dumps(graph))
    with pytest.raises(HTTPException) as exc: datasets.delete_dataset(item['id'])
    assert exc.value.status_code == 409 and 'Order project' in exc.value.detail
    assert datasets.load(item['id'])['records'] == item['records']
    assert datasets.public(item, True)['used_by'][0]['node'] == 'feed'
    path.unlink()
    datasets.delete_dataset(item['id'])
    assert not datasets.path_for(item['id']).exists()


def test_json_types_and_explicit_csv_conversion_are_input_local():
    item = dataset([{'amount':'12', 'tags':'["a"]', 'active':'false'}])
    config = {'dataset_id':item['id'], 'column_types':{'amount':'int','tags':'list','active':'bool'}}
    assert datasets.records(config) == [{'amount':12,'tags':['a'],'active':False}]
    assert datasets.load(item['id'])['records'] == item['records']
    typed = dataset([{'amount':12,'tags':['a'],'active':True}])
    assert datasets.public(typed)['field_types'] == {'amount':'int','tags':'list','active':'bool'}
    item['records'][0]['amount'] = '12.5'; datasets.save(item)
    with pytest.raises(datasets.SourceError, match="Record 1, column 'amount'"):
        datasets.records(config)


def test_generic_saved_results_choose_generic_metric_and_keep_inputs():
    assert saved.default_metric({'type':'saved_run','origin':{}}) == 'exact_match'
    # No project's source type picks a special metric: the platform default
    # holds whatever produced the saved results.
    assert saved.default_metric({'origin':{'type':'credit_risk'}}) == 'exact_match'
    captured = []
    class Model:
        def generate(self, prompt):
            captured.append(json.loads(prompt.split('unvalidated.\n',1)[1]))
            return type('Result',(),{'content':'{"prompts":{"a":"Classify {text}"}}'})()
    graph = {'tasks':[{'name':'a','prompt':'Read {text}'}]}
    records = [{'run_id':'r','inputs':{'customer_text':'important original material'},'status':'success','prediction':'yes','label':'no','nodes':[]}]
    saved.propose(graph,records,['a'],Model())
    assert captured[0]['records'][0]['inputs']['customer_text'] == 'important original material'
    assert 'company' not in captured[0]['records'][0]


def test_chat_canvas_restore_preserves_sessions_and_checks_ownership(monkeypatch):
    from backend.features.agents import harness_api as api
    graph = {'id':'restore-test'}
    monkeypatch.setattr(api, 'graph_for', lambda _:graph)
    agent = api.create_agent(graph['id'], api.AgentSettings(name='Research'))
    session = api.create_session(graph['id'], agent['id'])
    api.remove_agent(graph['id'], agent['id'])
    assert api.list_agents(graph['id'])['agents'] == []
    snapshot = api.CanvasSnapshot(agents=[api.AgentSnapshot(**agent)])
    api.restore_canvas(graph['id'], snapshot)
    assert api.list_agents(graph['id'])['agents'][0]['id'] == agent['id']
    with api.database() as db:
        assert api.get(db, session['id'], api.owner(graph), 'session')['id'] == session['id']
    monkeypatch.setitem(api._running, session['id'], object())
    with pytest.raises(HTTPException) as exc:
        api.restore_canvas(graph['id'], api.CanvasSnapshot(agents=[]))
    assert exc.value.status_code == 409
    monkeypatch.delitem(api._running, session['id'])
    monkeypatch.setattr(api, 'graph_for', lambda _:{'id':'different-task'})
    with pytest.raises(HTTPException) as exc: api.restore_canvas('different-task', snapshot)
    assert exc.value.status_code == 409
