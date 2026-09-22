import pytest
from fastapi.testclient import TestClient

@pytest.fixture
def client(tmp_path, monkeypatch):
    from backend.api import projects, graphs
    from backend.api.app import app
    monkeypatch.setattr(projects, 'PROJECTS_DIR', tmp_path/'projects')
    monkeypatch.setattr(graphs, 'GRAPHS_DIR', tmp_path/'graphs')
    return TestClient(app)


def test_generic_task_is_executable_and_membership_survives_edit_and_rename(client):
    from backend.api import graphs, run_plan
    p=client.post('/api/projects',json={'name':'Research'}).json()
    response=client.post(f'/api/projects/{p["id"]}/tasks',json={'name':'Read feedback','goal':'Summarize {customer} comments'})
    assert response.status_code==201,response.text
    task=response.json()
    assert task['tasks'][0]['tool_names']==[]
    assert task['tasks'][0]['system_prompt'].startswith('Summarize {customer}')
    assert run_plan.compile_plan(task)
    saved=graphs.save_graph(task['id'],{'name':'Renamed','tasks':task['tasks'],'edges':[]})
    assert saved['project_id']==p['id'] and saved['task_id']==task['task_id']
    listed=client.get(f'/api/projects/{p["id"]}/tasks').json()
    assert [x['id'] for x in listed]==[saved['id']]
    assert client.get('/api/projects/unassigned/tasks').json()==[]


def test_legacy_graph_requires_explicit_assignment(client):
    from backend.api import graphs
    g=graphs.create_graph('Existing risk workflow','')
    assert len(client.get('/api/projects/unassigned/tasks').json())==1
    p=client.post('/api/projects',json={'name':'Any project'}).json()
    assert client.get(f'/api/projects/{p["id"]}/tasks').json()==[]
    assert client.put(f'/api/projects/{p["id"]}/tasks/{g["id"]}').status_code==200
    assert client.get('/api/projects/unassigned/tasks').json()==[]


def test_invalid_project_and_blank_task_are_rejected(client):
    assert client.post('/api/projects',json={'name':'  '}).status_code==422
    assert client.get('/api/projects/unknown/tasks').status_code==404
    p=client.post('/api/projects',json={'name':'Research'}).json()
    assert client.post(f'/api/projects/{p["id"]}/tasks',json={'name':'Task','goal':' '}).status_code==422


def test_memory_canvas_resources_survive_save_without_becoming_tasks(client):
    from backend.api import graphs
    graph = graphs.create_graph('Canvas memory', '')
    resource = {'space_id': 'a' * 32, 'name': 'Shared research', 'x': 40, 'y': 60}
    graph.update(memory_resources=[resource], memory_positions={'mem:space:' + 'a' * 32: {'x': 80, 'y': 100}})
    saved = graphs.save_graph(graph['id'], graph)
    assert saved['memory_resources'] == [resource]
    assert saved['memory_positions'] == graph['memory_positions']
    assert saved['tasks'] == [] and saved['edges'] == []
    import pytest
    with pytest.raises(graphs.GraphValidationError):
        graphs.save_graph(graph['id'], {**graph, 'memory_resources': [resource, resource]})


def test_project_deletion_requires_empty_and_unassignment_preserves_task(client):
    p = client.post('/api/projects', json={'name': 'Temporary project'}).json()
    task = client.post(f'/api/projects/{p["id"]}/tasks', json={'name': 'Keep task', 'goal': 'Research'}).json()
    assert client.delete(f'/api/projects/{p["id"]}').status_code == 409
    moved = client.put(f'/api/projects/unassigned/tasks/{task["id"]}').json()
    assert moved['project_id'] is None and moved['task_id'] == task['task_id']
    assert client.delete(f'/api/projects/{p["id"]}').status_code == 200
    assert client.get('/api/projects').json() == []
    assert client.get('/api/graphs/' + task['id']).status_code == 200


def test_task_delete_rejects_active_work_then_clears_triggers(client, monkeypatch):
    from backend.api import graphs, runner, batch, harness_api, scheduler, watcher
    graph = graphs.create_graph('Delete test', '')
    monkeypatch.setattr(runner, 'list_runs', lambda **kw: [{'status': 'running'}])
    assert client.delete('/api/graphs/' + graph['id']).status_code == 409
    assert graphs.load_graph(graph['id']) is not None
    monkeypatch.setattr(runner, 'list_runs', lambda **kw: [])
    monkeypatch.setattr(batch, 'list_batches', lambda **kw: [])
    monkeypatch.setattr(harness_api, 'list_agents', lambda g: {'agents': []})
    stopped = []
    monkeypatch.setattr(scheduler, 'clear', lambda g: stopped.append(('schedule', g)))
    monkeypatch.setattr(watcher, 'stop_graph_watch', lambda g: stopped.append(('watch', g)))
    assert client.delete('/api/graphs/' + graph['id']).status_code == 200
    assert len(stopped) == 2
    assert graphs.load_graph(graph['id']) is None


def test_rename_project_preserves_identity_and_membership(client):
    p = client.post('/api/projects', json={'name': 'Before'}).json()
    task = client.post(f'/api/projects/{p["id"]}/tasks', json={'name': 'Task', 'goal': 'Research'}).json()
    renamed = client.put(f'/api/projects/{p["id"]}', json={'name': 'After', 'description': 'New description'})
    assert renamed.status_code == 200
    assert renamed.json()['id'] == p['id']
    assert client.get(f'/api/projects/{p["id"]}/tasks').json()[0]['id'] == task['id']


def test_task_can_be_created_without_a_project(client):
    result = client.post('/api/projects/unassigned/tasks', json={'name': 'Standalone', 'goal': 'Summarize feedback'})
    assert result.status_code == 201, result.text
    task = result.json()
    assert task['project_id'] is None
    assert client.get('/api/projects/unassigned/tasks').json()[0]['id'] == task['id']


def test_copy_task_has_independent_identity_and_preserves_definition(client, monkeypatch):
    from backend.api import graphs, harness_api
    p = client.post('/api/projects', json={'name':'Research'}).json()
    task = client.post(f'/api/projects/{p["id"]}/tasks', json={'name':'Original','goal':'Summarize'}).json()
    task['tasks'][0]['x'] = 123
    task = graphs.save_graph(task['id'], task)
    monkeypatch.setattr(harness_api, 'list_agents', lambda _: {'agents':[]})
    result = client.post(f'/api/projects/{p["id"]}/tasks/{task["id"]}/copy')
    assert result.status_code == 201, result.text
    copied = result.json()
    assert copied['id'] != task['id'] and copied['task_id'] != task['task_id']
    assert copied['tasks'] == task['tasks'] and copied['edges'] == task['edges']
    assert copied['project_id'] == p['id']
    assert copied['name'] == 'Original (copy)'
    assert graphs.load_graph(task['id']) == task
    copied['tasks'][0]['prompt'] = 'Changed'
    graphs.save_graph(copied['id'], copied)
    assert graphs.load_graph(task['id'])['tasks'][0]['prompt'] != 'Changed'
    again = client.post(f'/api/projects/{p["id"]}/tasks/{task["id"]}/copy').json()
    assert again['id'] != copied['id']


def test_assigning_legacy_task_preserves_chat_owner(client):
    from backend.api import graphs
    task=graphs.create_graph('Legacy chat owner','')
    p=client.post('/api/projects',json={'name':'Project'}).json()
    moved=client.put(f'/api/projects/{p["id"]}/tasks/{task["id"]}').json()
    assert moved['task_id']==task['id']
    renamed=graphs.rename_graph(moved['id'],'Renamed chat task')
    assert renamed['task_id']==task['id']


def test_renaming_unassigned_legacy_graph_retains_original_owner(client):
    from backend.api import graphs
    task=graphs.create_graph('Old chat owner','')
    renamed=graphs.rename_graph(task['id'],'New chat owner')
    assert renamed['task_id']==task['id']
