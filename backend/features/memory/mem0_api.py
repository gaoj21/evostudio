"""UI-facing Mem0 operations, scoped using the saved graph's project."""
import logging
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from backend.api import graphs, mem0_service as service

router = APIRouter(prefix='/api/graphs/{graph_id}/mem0', tags=['memory'])


class SpaceInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)

    @field_validator('name')
    @classmethod
    def nonblank(cls, value):
        if not value.strip(): raise ValueError('Name must not be blank')
        return value.strip()


class EntryInput(BaseModel):
    content: str = Field(min_length=1, max_length=20000)

    @field_validator('content')
    @classmethod
    def nonblank(cls, value):
        if not value.strip(): raise ValueError('Content must not be blank')
        return value


def graph_for(graph_id):
    if not graphs.graph_exists(graph_id):
        raise HTTPException(404, 'Task not found')
    return graphs.load_graph(graph_id)


def call(fn, *args):
    try:
        return fn(*args)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        logging.getLogger(__name__).exception('Mem0 operation failed')
        raise HTTPException(503, 'Mem0 operation failed. Check the local embedding model and server logs.') from exc


@router.get('/spaces')
def list_spaces(graph_id: str):
    graph = graph_for(graph_id)
    return {'spaces': service.spaces(graph), 'status': service.status()}


@router.post('/spaces', status_code=201)
def create_space(graph_id: str, body: SpaceInput):
    return call(service.create_space, graph_for(graph_id), body.name)


@router.get('/spaces/{space_id}/entries')
def list_entries(graph_id: str, space_id: str, q: str | None = None, limit: int = Query(100, ge=1, le=1000)):
    return {'entries': call(service.entries, graph_for(graph_id), space_id, q, limit)}


@router.post('/spaces/{space_id}/entries', status_code=201)
def add_entry(graph_id: str, space_id: str, body: EntryInput):
    return call(service.add, graph_for(graph_id), space_id, body.content)


@router.put('/spaces/{space_id}/entries/{memory_id}')
def update_entry(graph_id: str, space_id: str, memory_id: str, body: EntryInput):
    return call(service.mutate, graph_for(graph_id), space_id, memory_id, body.content)


@router.delete('/spaces/{space_id}/entries/{memory_id}')
def delete_entry(graph_id: str, space_id: str, memory_id: str):
    return call(service.mutate, graph_for(graph_id), space_id, memory_id)


@router.delete('/spaces/{space_id}')
def delete_space(graph_id: str, space_id: str):
    from backend.api import harness_api
    graph = graph_for(graph_id)
    call(service.get_space, graph, space_id)
    # A space is shared across the project. Check every saved task, including
    # canvas-only placements, before allowing its underlying storage to go away.
    for summary in graphs.list_graphs():
        candidate = graphs.load_graph(summary['id'])
        if not candidate: continue
        references = [r.get('space_id') for r in candidate.get('memory_resources', [])]
        references += [(t.get('memory') or {}).get('space_id') for t in candidate.get('tasks', [])]
        for agent in harness_api.list_agents(candidate['id'])['agents']:
            references += [m.get('space_id') or str(m.get('memory_id', '')).removeprefix('mem:space:') for m in agent.get('memories', [])]
        if space_id in references:
            raise HTTPException(409, 'Remove this space from all canvases and disconnect its Agents before deleting it.')
    with service._lock:
        try: service.client().delete_all(user_id=space_id)
        except Exception as exc:
            logging.getLogger(__name__).exception('Mem0 space deletion failed')
            raise HTTPException(503, 'Memory storage could not be deleted. Please retry.') from exc
        (service.ROOT / 'spaces' / (space_id + '.json')).unlink()
    return {'removed': True}
