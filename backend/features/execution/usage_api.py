"""Where a run's or a batch's tokens went, for the Usage tab.

One call answers the three questions the tab asks: how much in total (and
what it cost), which node spent it, and — for a batch — which record. Every
figure is what the provider reported; a node or record that reported nothing
says so rather than showing a zero. Costs come from the machine's own `llm`
package pricing (token_usage.priced), never from a price written here.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from backend.features.execution import token_usage

router = APIRouter(prefix="/api", tags=["Usage"])

RUNNING = ("pending", "queued", "running", "cancelling", "stopping")


def _entry(usage: dict | None) -> dict:
    """A usage figure with its cost, or an explicit 'not reported' — with
    how many calls came back without a report, when there were any."""
    unreported = (usage or {}).get("unreported_calls")
    extra = {"unreported_calls": unreported} if unreported else {}
    if not usage or not usage.get("reported_calls"):
        return {"reported": False, **extra}
    return {"reported": True, **usage, "cost": token_usage.priced(usage)}


def _share(rows: list[dict], total: dict | None) -> list[dict]:
    whole = (total or {}).get("total_tokens") or 0
    for row in rows:
        spent = row["usage"].get("total_tokens") or 0
        row["share"] = round(spent / whole, 4) if whole and row["usage"].get("reported") else None
    return rows


def run_usage(run: dict) -> dict:
    nodes = [{"node": node.get("name"), "status": node.get("status"),
              "usage": _entry(node.get("token_usage"))}
             for node in run.get("nodes") or [] if node.get("status") != "skipped"]
    total = run.get("token_usage")
    return {"kind": "run", "id": run.get("run_id"), "status": run.get("status"),
            "running": run.get("status") in RUNNING,
            "total": _entry(total), "by_node": _share(nodes, total), "by_record": None}


def batch_usage(batch: dict) -> dict:
    progress = batch.get("node_progress") or {}
    nodes = [{"node": name, "status": None, "usage": _entry((entry or {}).get("token_usage"))}
             for name, entry in progress.items()]
    records = [{"index": item.get("index"), "run_id": item.get("run_id"),
                "status": item.get("status"), "usage": _entry(item.get("token_usage"))}
               for item in batch.get("items") or []]
    total = batch.get("token_usage")
    return {"kind": "batch", "id": batch.get("batch_id"), "status": batch.get("status"),
            "running": batch.get("status") in RUNNING,
            "total": _entry(total), "by_node": _share(nodes, total),
            "by_record": _share(records, total)}


@router.get("/usage")
def usage(run_id: str | None = Query(default=None), batch_id: str | None = Query(default=None)):
    """Usage of one run (`run_id`) or one batch (`batch_id`), live while it runs."""
    if bool(run_id) == bool(batch_id):
        raise HTTPException(422, "Give exactly one of run_id or batch_id.")
    if batch_id:
        from backend.api import batch as batch_store
        batch = batch_store.get_batch(batch_id)
        if batch is None:
            raise HTTPException(404, f"Batch '{batch_id}' not found")
        return batch_usage(batch)
    from backend.api import runner
    run = runner.get_run(run_id)
    if run is None:
        raise HTTPException(404, f"Run '{run_id}' not found")
    return run_usage(run)
