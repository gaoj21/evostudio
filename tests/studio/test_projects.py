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
    import threading
    from backend.api import graphs, runner, batch, harness_api, scheduler, watcher
    graph = graphs.create_graph('Delete test', '')
    monkeypatch.setattr(runner, 'list_runs', lambda **kw: [{'status': 'running'}])
    assert client.delete('/api/graphs/' + graph['id']).status_code == 409
    assert graphs.load_graph(graph['id']) is not None
    monkeypatch.setattr(runner, 'list_runs', lambda **kw: [])
    monkeypatch.setattr(batch, 'list_batches', lambda **kw: [])
    agent = 'e1' * 16
    graphs.save_graph(graph['id'], {**graphs.load_graph(graph['id']), 'tasks': [
        {'name': 'helper', 'kind': 'chat_agent', 'agent_id': agent}]})
    session = harness_api.create_session(graph['id'], agent)
    monkeypatch.setitem(harness_api._running, session['id'], threading.Event())
    busy = client.delete('/api/graphs/' + graph['id'])
    assert busy.status_code == 409 and 'conversations' in busy.json()['detail']
    monkeypatch.delitem(harness_api._running, session['id'])
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
    agent = 'f2' * 16
    task['tasks'].append({'name': 'helper', 'kind': 'chat_agent', 'agent_id': agent, 'instructions': 'Help.'})
    task = graphs.save_graph(task['id'], task)
    session = harness_api.create_session(task['id'], agent)
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
    # The Chat Agent came along as a node; its conversations did not.
    assert harness_api.list_sessions(copied['id'], agent)['sessions'] == []
    harness_api.create_session(copied['id'], agent)
    assert [s['id'] for s in harness_api.list_sessions(task['id'], agent)['sessions']] == [session['id']]


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


@pytest.fixture
def bound_task(client, tmp_path, monkeypatch):
    """A task in one project whose canvas, node and Chat Agent use that
    project's shared memory."""
    from backend.api import graphs, mem0_service
    monkeypatch.setattr(mem0_service, 'ROOT', tmp_path / 'mem0')
    home = client.post('/api/projects', json={'name': 'Home'}).json()
    away = client.post('/api/projects', json={'name': 'Away'}).json()
    task = client.post(f'/api/projects/{home["id"]}/tasks', json={'name': 'Bound', 'goal': 'Do {input}'}).json()
    space = mem0_service.create_space(task, 'Team notes')
    node = {**task['tasks'][0], 'use_long_term_memory': True,
            'memory': {'provider': 'mem0', 'space_id': space['id'], 'read_enabled': True, 'write_enabled': True}}
    task = graphs.save_graph(task['id'], {**task, 'tasks': [node],
                                          'memory_resources': [{'space_id': space['id'], 'name': 'Team notes', 'x': 0, 'y': 0}],
                                          'memory_positions': {'mem:space:' + space['id']: {'x': 1, 'y': 2}}})
    agent = {'name': 'helper', 'kind': 'chat_agent', 'agent_id': 'b7' * 16,
             'memories': [{'space_id': space['id'], 'read': True, 'write': True}]}
    task = graphs.save_graph(task['id'], {**task, 'tasks': [*task['tasks'], agent]})
    return client, task, agent, space, home, away


def _agents(graph_id):
    """The workflow's Chat Agent nodes, as saved."""
    from backend.api import graphs
    return [t for t in graphs.load_graph(graph_id)['tasks'] if t.get('kind') == 'chat_agent']


def test_moving_a_task_away_from_its_shared_memory_asks_first(bound_task):
    from backend.api import graphs
    client, task, agent, space, home, away = bound_task
    refused = client.put(f'/api/projects/{away["id"]}/tasks/{task["id"]}')
    assert refused.status_code == 409
    detail = refused.json()['detail']
    assert 'Away' in detail['message']
    assert sorted((b['kind'], b['name'], b['space']) for b in detail['bindings']) == [
        ('agent', 'helper', 'Team notes'), ('canvas', 'Team notes', 'Team notes'), ('node', 'execute', 'Team notes')]
    # Nothing moved and nothing was unbound.
    assert graphs.load_graph(task['id'])['project_id'] == home['id']
    assert _agents(task['id'])[0]['memories']


def test_detaching_moves_the_task_and_removes_its_bindings(bound_task):
    from backend.api import graphs
    client, task, agent, space, home, away = bound_task
    moved = client.put(f'/api/projects/{away["id"]}/tasks/{task["id"]}', params={'detach': 'true'})
    assert moved.status_code == 200, moved.text
    body = moved.json()
    assert body['project_id'] == away['id']
    assert {b['kind'] for b in body['detached']} == {'agent', 'canvas', 'node'}
    saved = graphs.load_graph(task['id'])
    assert saved['memory_resources'] == [] and saved['memory_positions'] == {}
    node = saved['tasks'][0]
    assert node['use_long_term_memory'] is False and 'space_id' not in node['memory']
    assert _agents(task['id'])[0]['memories'] == []


def test_a_move_within_the_same_scope_or_without_shared_memory_needs_no_detach(bound_task):
    client, task, agent, space, home, away = bound_task
    assert client.put(f'/api/projects/{home["id"]}/tasks/{task["id"]}').status_code == 200
    plain = client.post(f'/api/projects/{home["id"]}/tasks', json={'name': 'Plain', 'goal': 'Go'}).json()
    moved = client.put(f'/api/projects/{away["id"]}/tasks/{plain["id"]}')
    assert moved.status_code == 200 and moved.json()['detached'] == []


def test_a_stale_binding_is_named_when_chatting_and_can_be_removed(bound_task, monkeypatch):
    from backend.api import graphs
    client, task, agent, space, home, away = bound_task
    from backend.api import harness_api
    # Moved before moves checked shared memory: the binding is out of reach.
    graphs.save_graph(task['id'], {**graphs.load_graph(task['id']), 'project_id': away['id']})
    assert _agents(task['id'])[0]['memories'][0]['space_id'] == space['id']
    monkeypatch.setattr(harness_api._pool, 'submit', lambda *a: None)
    monkeypatch.setattr(harness_api.harness, 'make_model', lambda provider: object())
    monkeypatch.setattr(harness_api, '_running', {})
    root = f'/api/graphs/{task["id"]}/agents/{agent["agent_id"]}/sessions'
    turn = f'{root}/{client.post(root).json()["id"]}/messages'
    kept = client.post(turn, json={'message': 'hi'})
    assert kept.status_code == 422
    message = kept.json()['detail']
    assert "'Team notes'" in message and space['id'] in message and 'Remove it under Connected memories' in message
    saved = graphs.load_graph(task['id'])
    graphs.save_graph(task['id'], {**saved, 'tasks': [
        {**t, 'memories': []} if t.get('kind') == 'chat_agent' else t for t in saved['tasks']]})
    assert client.post(turn, json={'message': 'hi'}).status_code == 202
    # A later move does not report what was already out of reach.
    moved = client.put(f'/api/projects/{home["id"]}/tasks/{task["id"]}')
    assert moved.status_code == 200


def test_copying_into_another_project_asks_before_dropping_shared_memory(bound_task):
    from backend.api import graphs
    client, task, agent, space, home, away = bound_task
    before = {g['id'] for g in graphs.list_graphs()}
    refused = client.post(f'/api/projects/{away["id"]}/tasks/{task["id"]}/copy')
    assert refused.status_code == 409, refused.text
    detail = refused.json()['detail']
    assert 'Away' in detail['message']
    assert sorted((b['kind'], b['name']) for b in detail['bindings']) == [
        ('agent', 'helper'), ('canvas', 'Team notes'), ('node', 'execute')]
    assert {g['id'] for g in graphs.list_graphs()} == before
    copied = client.post(f'/api/projects/{away["id"]}/tasks/{task["id"]}/copy', params={'detach': 'true'})
    assert copied.status_code == 201, copied.text
    body = copied.json()
    assert body['project_id'] == away['id'] and {b['kind'] for b in body['detached']} == {'agent', 'canvas', 'node'}
    saved = graphs.load_graph(body['id'])
    assert saved['memory_resources'] == [] and saved['tasks'][0]['use_long_term_memory'] is False
    assert [(a['name'], a['memories']) for a in _agents(body['id'])] == [('helper', [])]
    # The original keeps everything.
    assert _agents(task['id'])[0]['memories']
    assert graphs.load_graph(task['id'])['memory_resources']


def test_copying_out_of_a_project_or_within_it(bound_task):
    from backend.api import graphs
    client, task, agent, space, home, away = bound_task
    # Unassigned copies get a scope of their own: the project's memory is out of reach.
    assert client.post(f'/api/projects/unassigned/tasks/{task["id"]}/copy').status_code == 409
    loose = client.post(f'/api/projects/unassigned/tasks/{task["id"]}/copy', params={'detach': 'true'})
    assert loose.status_code == 201 and loose.json()['project_id'] is None
    # Within the project the copy keeps its shared memory.
    same = client.post(f'/api/projects/{home["id"]}/tasks/{task["id"]}/copy')
    assert same.status_code == 201, same.text
    assert same.json()['detached'] == []
    assert graphs.load_graph(same.json()['id'])['memory_resources'][0]['space_id'] == space['id']
    assert _agents(same.json()['id'])[0]['memories'][0]['space_id'] == space['id']
    # An unassigned task's private memory cannot follow its copy into a project.
    private = graphs.create_graph('Private', '')
    from backend.api import mem0_service
    own = mem0_service.create_space(private, 'Mine')
    private = graphs.save_graph(private['id'], {**private, 'memory_resources': [{'space_id': own['id'], 'name': 'Mine', 'x': 0, 'y': 0}],
                                                'tasks': [{'name': 'solo', 'kind': 'chat_agent', 'memories': [{'space_id': own['id']}]}]})
    assert client.post(f'/api/projects/{home["id"]}/tasks/{private["id"]}/copy').status_code == 409
    into = client.post(f'/api/projects/{home["id"]}/tasks/{private["id"]}/copy', params={'detach': 'true'})
    assert into.status_code == 201, into.text
