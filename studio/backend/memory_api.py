"""Memory browsing API for EvoAgentX Studio.

Exposes per-agent long-term memory stores (written by runs when a task has
use_long_term_memory=true) as REST endpoints for the frontend Memory panel.
"""

from fastapi import APIRouter, HTTPException, Query

import graphs as graph_store
import memory_store

router = APIRouter(prefix="/api")


@router.get("/graphs/{graph_id}/memory/agents")
def list_memory_agents(graph_id: str):
    if not graph_store.graph_exists(graph_id):
        raise HTTPException(status_code=404, detail=f"Graph '{graph_id}' not found")
    return {"agents": memory_store.list_agents(graph_id)}


@router.get("/graphs/{graph_id}/memory")
def search_memory(
    graph_id: str,
    agent: str = Query(...),
    q: str | None = Query(default=None),
    n: int = Query(default=10, ge=1, le=100),
):
    if not graph_store.graph_exists(graph_id):
        raise HTTPException(status_code=404, detail=f"Graph '{graph_id}' not found")
    try:
        if q:
            memory = memory_store.open_memory(graph_id, agent)
            if memory is None:
                return {"agent": agent, "query": q, "entries": []}
            hits = memory.search(q, n=n)
            entries = [
                {
                    "memory_id": memory_id,
                    "content": memory_store.unquote_content(msg.content),
                    "timestamp": msg.timestamp,
                    "agent": msg.agent,
                    "wf_task": msg.wf_task,
                    "wf_goal": msg.wf_goal,
                    "msg_type": msg.msg_type.value if msg.msg_type else None,
                }
                for msg, memory_id in hits
            ]
        else:
            entries = memory_store.list_entries(graph_id, agent)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read memory store: {e}")
    return {"agent": agent, "query": q, "entries": entries}
