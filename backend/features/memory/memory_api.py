"""Memory browsing API for EvoAgentX Studio.

Exposes per-agent long-term memory stores (written by runs when a task has
use_long_term_memory=true) as REST endpoints for the frontend Memory panel.
"""

from fastapi import APIRouter, HTTPException, Query

from backend.api import graphs as graph_store
from backend.api import memory_reset, memory_store, table_store
router = APIRouter(prefix="/api")


def _stores(graph_id: str) -> list[dict]:
    """Every store a run of this workflow wrote, whichever kind.

    The panel listed only the vector corpora, so a workflow whose nodes keep
    tables — one row per company per date, the credit-risk pipeline's whole
    memory — showed "(no memory stores yet)" with 41 rows on disk.
    """
    out = [{"node": node, "kind": "table",
            "count": table_store.count(graph_id, node),
            "subjects": table_store.subjects(graph_id, node)}
           for node in table_store.nodes(graph_id)]
    out += [{"node": agent, "kind": "recall", "count": None, "subjects": []}
            for agent in memory_store.list_agents(graph_id)]
    return out


@router.get("/graphs/{graph_id}/memory/agents")
def list_memory_agents(graph_id: str):
    if not graph_store.graph_exists(graph_id):
        raise HTTPException(status_code=404, detail=f"Graph '{graph_id}' not found")
    stores = _stores(graph_id)
    # `agents` kept for callers that only knew corpora; `stores` is the list.
    return {"agents": [s["node"] for s in stores if s["kind"] == "recall"],
            "stores": stores}


@router.get("/graphs/{graph_id}/memory")
def search_memory(
    graph_id: str,
    agent: str = Query(...),
    q: str | None = Query(default=None),
    n: int = Query(default=10, ge=1, le=100),
):
    if not graph_store.graph_exists(graph_id):
        raise HTTPException(status_code=404, detail=f"Graph '{graph_id}' not found")
    if agent in table_store.nodes(graph_id):
        # A table is browsed, not searched: `q` narrows to subjects whose
        # name contains it. Oldest first within a subject — a timeline.
        needle = (q or "").strip().lower()
        rows = [r for r in table_store.rows(graph_id, agent)
                if not needle or needle in str(r["subject"]).lower()]
        rows.sort(key=lambda r: (str(r["subject"]), str(r["at"])))
        return {"agent": agent, "kind": "table", "query": q,
                "entries": [{"subject": r["subject"], "at": r["at"],
                             "recorded_at": r["recorded_at"], "content": r["payload"]}
                            for r in rows][:n if n > 10 else 1000]}
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
    return {"agent": agent, "kind": "recall", "query": q, "entries": entries}


def _mem0_guard(graph_id: str | None) -> None:
    """A shared Mem0 space is not this workflow's to empty."""
    from backend.api import mem0_service

    if graph_id and graph_store.graph_exists(graph_id):
        graph = graph_store.load_graph(graph_id)
        if any(t.get('use_long_term_memory') and (t.get('memory') or {}).get('provider') == 'mem0'
               for t in graph.get('tasks', [])):
            raise HTTPException(409, 'This task uses shared Mem0 memory. For a clean experiment, select a new empty space; workflow reset cannot clear a shared space.')
    elif not graph_id and any((mem0_service.ROOT / 'spaces').glob('*.json')):
        raise HTTPException(409, 'Shared Mem0 spaces exist. Reset individual workflows or manage shared entries in the Mem0 panel.')


def _live() -> bool:
    from backend.api import batch as batch_store
    from backend.api import runner

    return any(b.get("status") in ("running", "cancelling") for b in batch_store.list_batches()) \
        or any(r.get("status") == "running" for r in runner.list_runs())


@router.post("/memory/backup")
def backup_memory(body: dict | None = None):
    """Copy memory to a timestamped backup and leave it in place.

    Per workflow when `graph_id` is given, otherwise every store. Allowed
    while things run: a backup takes nothing away.
    """
    body = body or {}
    return memory_reset.snapshot(body.get("graph_id") or None)


@router.post("/memory/reset")
def reset_memory(body: dict | None = None):
    """Empty memory. Backs up first unless `backup` is false.

    Per workflow when `graph_id` is given, otherwise every store. Refused
    while a run or batch is executing — clearing memory under a live run
    would leave it reading from nothing halfway through.
    """
    body = body or {}
    graph_id = body.get('graph_id') or None
    _mem0_guard(graph_id)
    try:
        return memory_reset.clear(graph_id, busy=_live(),
                                  backup=body.get("backup", True) is not False)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("/memory/backups")
def list_memory_backups():
    return {"backups": memory_reset.backups()}
