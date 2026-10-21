"""Project organization for domain-independent tasks backed by executable graphs."""
import json
import re
import threading
import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator
from backend.api.studio_config import DATA_DIR
from backend.api import graphs
from backend.features.persistence import write_json

PROJECTS_DIR = DATA_DIR / 'projects'
router = APIRouter(prefix='/api/projects', tags=['projects'])
_lock = threading.RLock()


class ProjectInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default='', max_length=4000)

    @field_validator('name')
    @classmethod
    def nonblank(cls, value):
        if not value.strip(): raise ValueError('Name must not be blank')
        return value.strip()


class TaskInput(ProjectInput):
    goal: str = Field(min_length=1, max_length=12000)
    output_description: str = Field(default='A clear answer with supporting evidence.', max_length=4000)

    @field_validator('goal')
    @classmethod
    def goal_nonblank(cls, value):
        if not value.strip(): raise ValueError('Goal must not be blank')
        return value.strip()


def _path(project_id):
    if not re.fullmatch(r'[a-f0-9]{32}', project_id):
        raise HTTPException(404, 'Project not found')
    return PROJECTS_DIR / (project_id + '.json')


def get_project(project_id):
    try: return json.loads(_path(project_id).read_text(encoding='utf-8'))
    except FileNotFoundError: raise HTTPException(404, 'Project not found')


@router.get('')
def list_projects():
    result=[]
    for p in PROJECTS_DIR.glob('*.json'):
        try: item = json.loads(p.read_text(encoding='utf-8'))
        except (OSError, ValueError): continue
        if isinstance(item, dict): result.append(item)
    return sorted(result, key=lambda x:str(x.get('updated_at') or ''), reverse=True)


@router.post('', status_code=201)
def create_project(body: ProjectInput):
    with _lock:
        project={'id':uuid.uuid4().hex, **body.model_dump(),
                 'updated_at':datetime.now(timezone.utc).isoformat()}
        write_json(_path(project['id']), project)
    return project


@router.get('/{project_id}/tasks')
def list_tasks(project_id: str):
    if project_id!='unassigned':get_project(project_id)
    return [g for g in graphs.list_graphs()
            if g.get('project_id') == (None if project_id=='unassigned' else project_id)]


@router.post('/{project_id}/tasks', status_code=201)
def create_task(project_id: str, body: TaskInput):
    if project_id != 'unassigned': get_project(project_id)
    # Braces in a user's goal are literal system instructions, not template fields.
    node={'name':'execute','description':body.goal,
          'system_prompt':body.goal+'\nExpected output: '+body.output_description,
          'prompt':'Complete the task using this input:\n{input}',
          'inputs':[{'name':'input','type':'str','description':'Material or instructions for this run','required':True}],
          'outputs':[{'name':'result','type':'str','description':body.output_description,'required':True}],
          'parse_mode':'str','tool_names':[], 'skill_names':[]}
    candidate={'name':body.name,'goal':body.goal,'tasks':[node],'edges':[]}
    graphs.validate_graph(candidate)
    graph=graphs.create_graph(body.name,body.goal)
    try:
        return graphs.save_graph(graph['id'],{**graph,**candidate,'project_id':None if project_id == 'unassigned' else project_id,'task_id':uuid.uuid4().hex})
    except Exception:
        graphs.delete_graph(graph['id']);raise


def memory_uses(graph):
    """Every Mem0 space the task refers to, with where: canvas, node or Chat Agent."""
    from backend.features.agents.chat_agents import is_chat_agent, space_of
    uses = []
    for resource in graph.get('memory_resources') or []:
        uses.append((resource.get('space_id'), {'kind': 'canvas', 'name': resource.get('name')}))
    for task in graph.get('tasks') or []:
        policy = task.get('memory') or {}
        if policy.get('provider') == 'mem0' and policy.get('space_id'):
            uses.append((policy['space_id'], {'kind': 'node', 'name': task.get('name')}))
        if is_chat_agent(task):
            for binding in task.get('memories') or []:
                uses.append((space_of(binding), {'kind': 'agent', 'name': task.get('name'), 'agent_id': task.get('agent_id')}))
    return [(space_id, use) for space_id, use in uses if space_id]


def lost_memory_bindings(graph, moved):
    """Mem0 spaces this task uses now that it could not reach after the move.

    Shared memory is scoped to a project (or to an unassigned task), so a
    move leaves behind every space of the old scope. Bindings that were
    already out of reach are not the move's doing and are not listed."""
    from backend.api import mem0_service
    before, after = mem0_service.scope(graph), mem0_service.scope(moved)
    if before == after: return []
    lost = []
    for space_id, use in memory_uses(graph):
        record = mem0_service.space_record(space_id)
        if record and record.get('scope') == before:
            lost.append({**use, 'space_id': space_id, 'space': record.get('name') or space_id})
    return lost


def unreachable_memory_bindings(graph, copied):
    """Mem0 spaces a copy could not use. A copy is a new task: whatever its
    scope cannot reach is listed, since its Agents could not be created with it."""
    from backend.api import mem0_service
    scope = mem0_service.scope(copied)
    lost = []
    for space_id, use in memory_uses(graph):
        record = mem0_service.space_record(space_id)
        if not record or record.get('scope') != scope:
            lost.append({**use, 'space_id': space_id, 'space': (record or {}).get('name') or space_id})
    return lost


def _destination_name(project_id):
    return get_project(project_id)['name'] if project_id != 'unassigned' else 'Unassigned tasks'


def detach_memory_bindings(graph, space_ids):
    """The graph without these spaces, unbound the way the canvas unbinds them."""
    from backend.features.agents.chat_agents import is_chat_agent, space_of
    tasks = []
    for task in graph.get('tasks') or []:
        if is_chat_agent(task):
            task = {**task, 'memories': [m for m in task.get('memories') or [] if space_of(m) not in space_ids]}
        policy = task.get('memory') or {}
        if policy.get('provider') == 'mem0' and policy.get('space_id') in space_ids:
            policy = {k: v for k, v in policy.items() if k != 'space_id'}
            task = {**task, 'use_long_term_memory': False,
                    'memory': {**policy, 'read_enabled': False, 'write_enabled': False}}
        tasks.append(task)
    resources = [r for r in graph.get('memory_resources') or [] if r.get('space_id') not in space_ids]
    positions = {k: v for k, v in (graph.get('memory_positions') or {}).items()
                 if k.removeprefix('mem:space:') not in space_ids}
    return {**graph, 'tasks': tasks, 'memory_resources': resources, 'memory_positions': positions}


@router.put('/{project_id}/tasks/{graph_id}')
def assign_task(project_id: str, graph_id: str, detach: bool = False):
    """Move a task. Shared memory it uses from the old project stays there:
    without `detach` the move is refused (409) with the bindings it would
    lose; with it they are removed and listed under `detached`."""
    if project_id != 'unassigned': get_project(project_id)
    if not re.fullmatch(r"[a-z0-9_-]+", graph_id): raise HTTPException(404, "Task not found")
    with _lock:
        graph=graphs.load_graph(graph_id)
        if not graph:raise HTTPException(404,'Task not found')
        moved={**graph,'project_id':None if project_id == 'unassigned' else project_id,'task_id':graph.get('task_id') or graph['id']}
        lost=lost_memory_bindings(graph, moved)
        if lost and not detach:
            destination = _destination_name(project_id)
            raise HTTPException(409, {
                'message': f"This task uses shared memory that stays with its current project and will not be available in {destination}.",
                'destination': destination, 'bindings': lost})
        space_ids={item['space_id'] for item in lost}
        saved=graphs.save_graph(graph_id, detach_memory_bindings(moved, space_ids) if space_ids else moved)
    return {**saved, 'detached': lost}


@router.delete('/{project_id}')
def delete_project(project_id: str):
    with _lock:
        get_project(project_id)
        if list_tasks(project_id): raise HTTPException(409, 'Move or delete the tasks in this project first.')
        _path(project_id).unlink()
    return {'removed': True}


@router.put('/{project_id}')
def update_project(project_id: str, body: ProjectInput):
    with _lock:
        project = {**get_project(project_id), **body.model_dump(), 'updated_at': datetime.now(timezone.utc).isoformat()}
        write_json(_path(project_id), project)
    return project


@router.post('/{project_id}/tasks/{graph_id}/copy', status_code=201)
def copy_task(project_id: str, graph_id: str, detach: bool = False):
    """Copy definitions with a fresh execution identity; resources remain references.

    Shared memory the copy's scope cannot reach cannot come along: without
    `detach` the copy is refused (409) with those bindings; with it the copy
    is made without them and they are listed under `detached`."""
    import copy
    if project_id != 'unassigned': get_project(project_id)
    if not re.fullmatch(r'[a-z0-9_-]+', graph_id): raise HTTPException(404, 'Task not found')
    original = graphs.load_graph(graph_id)
    if not original: raise HTTPException(404, 'Task not found')
    task_id = uuid.uuid4().hex
    target = {**original, 'project_id': None if project_id == 'unassigned' else project_id, 'task_id': task_id}
    lost = unreachable_memory_bindings(original, target)
    if lost and not detach:
        destination = _destination_name(project_id)
        raise HTTPException(409, {
            'message': f"This task uses shared memory that will not be available to its copy in {destination}.",
            'destination': destination, 'bindings': lost})
    space_ids = {item['space_id'] for item in lost}
    name = f"{original['name']} (copy)"
    created = graphs.create_graph(name, original.get('goal', ''))
    try:
        body = {**copy.deepcopy(target), 'id': created['id'], 'name': name}
        # Its Chat Agents come along as nodes; the copy's own task_id keys
        # their conversations apart from the original's.
        cloned = graphs.save_graph(created['id'], detach_memory_bindings(body, space_ids) if space_ids else body)
        return {**cloned, 'detached': lost}
    except Exception:
        # No partial task should appear after an unsuccessful copy.
        graphs.delete_graph(created['id'])
        raise
