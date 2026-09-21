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
    try: return json.loads(_path(project_id).read_text())
    except FileNotFoundError: raise HTTPException(404, 'Project not found')


@router.get('')
def list_projects():
    result=[]
    for p in PROJECTS_DIR.glob('*.json'):
        try: result.append(json.loads(p.read_text()))
        except (OSError, ValueError): continue
    return sorted(result, key=lambda x:x['updated_at'], reverse=True)


@router.post('', status_code=201)
def create_project(body: ProjectInput):
    with _lock:
        project={'id':uuid.uuid4().hex, **body.model_dump(),
                 'updated_at':datetime.now(timezone.utc).isoformat()}
        PROJECTS_DIR.mkdir(parents=True,exist_ok=True)
        p=_path(project['id']);temp=p.with_suffix('.tmp')
        temp.write_text(json.dumps(project,ensure_ascii=False,indent=2));temp.replace(p)
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


@router.put('/{project_id}/tasks/{graph_id}')
def assign_task(project_id: str, graph_id: str):
    if project_id != 'unassigned': get_project(project_id)
    if not re.fullmatch(r"[a-z0-9_-]+", graph_id): raise HTTPException(404, "Task not found")
    graph=graphs.load_graph(graph_id)
    if not graph:raise HTTPException(404,'Task not found')
    return graphs.save_graph(graph_id,{**graph,'project_id':None if project_id == 'unassigned' else project_id,'task_id':graph.get('task_id') or uuid.uuid4().hex})


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
        path = _path(project_id)
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(project, ensure_ascii=False, indent=2))
        temp.replace(path)
    return project


@router.post('/{project_id}/tasks/{graph_id}/copy', status_code=201)
def copy_task(project_id: str, graph_id: str):
    """Copy definitions with a fresh execution identity; resources remain references."""
    import copy
    from backend.api import harness_api
    if project_id != 'unassigned': get_project(project_id)
    if not re.fullmatch(r'[a-z0-9_-]+', graph_id): raise HTTPException(404, 'Task not found')
    original = graphs.load_graph(graph_id)
    if not original: raise HTTPException(404, 'Task not found')
    agents = harness_api.list_agents(graph_id)['agents']
    name = f"{original['name']} (copy)"
    created = graphs.create_graph(name, original.get('goal', ''))
    try:
        cloned = graphs.save_graph(created['id'], {
            **copy.deepcopy(original), 'id':created['id'], 'name':name,
            'project_id':None if project_id == 'unassigned' else project_id,
            'task_id':uuid.uuid4().hex})
        for agent in agents:
            settings = {k:v for k,v in agent.items() if k in harness_api.AgentSettings.model_fields}
            harness_api.create_agent(cloned['id'], harness_api.AgentSettings(**settings))
        return cloned
    except Exception:
        # No partial task should appear after an unsuccessful copy.
        for agent in harness_api.list_agents(created['id'])['agents']:
            harness_api.remove_agent(created['id'], agent['id'])
        graphs.delete_graph(created['id'])
        raise
