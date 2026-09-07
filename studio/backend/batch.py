"""Batch run engine for EvoAgentX Studio.

A batch runs a list of input records concurrently (ThreadPoolExecutor,
workers configurable 1-5); each record is a normal run via
runner.start_run in synchronous mode. State lives in a module-level dict
and is persisted to studio/data/batches/{batch_id}.json on completion.
"""

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import runner
from .studio_config import data_path

BATCHES_DIR = data_path("batches")

MAX_WORKERS = 5

_batches: dict[str, dict] = {}
# The worker thread for each batch, so a caller can wait for one to wind down.
# A batch persists itself from that thread after its last item settles, which
# is otherwise unobservable from outside.
_threads: dict[str, threading.Thread] = {}
_lock = threading.Lock()

_SUMMARY_CHARS = 500


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _summarize(text, limit: int = _SUMMARY_CHARS) -> str:
    text = str(text)
    return text if len(text) <= limit else text[:limit] + "…"


def start_batch(graph: dict, records: list[dict], source: dict, gray_zone=None,
                workers: int = 2, metric: str | None = None,
                labels: list | None = None) -> str:
    """Start a batch over pre-mapped input records; returns the batch_id.

    With `metric` and `labels`, the batch is an evaluation: each item is scored
    as it finishes and the batch carries an aggregate. Scoring rides along with
    the ordinary batch so node progress, review routing and artifacts behave
    exactly as they do in a plain run.
    """
    batch_id = uuid.uuid4().hex[:12]
    workers = max(1, min(int(workers or 2), MAX_WORKERS))
    state = {
        "batch_id": batch_id,
        "graph_id": graph.get("id"),
        "status": "running",
        "source": source,
        "metric": metric,
        "summary": None,
        "gray_zone": list(gray_zone) if gray_zone else None,
        "workers": workers,
        "total": len(records),
        "items": [
            {"index": i, "status": "pending", "run_id": None,
             "inputs": record, "output_summary": None, "error": None,
             "review_status": None,
             "label": (labels[i] if labels and i < len(labels) else None),
             "score": None, "score_detail": None}
            for i, record in enumerate(records)
        ],
        "created_at": _utcnow(),
        "cancelled_items": 0,
    }
    with _lock:
        _batches[batch_id] = state
    # Persisted before execution: a batch the process never finishes would
    # otherwise disappear, leaving the canvas polling an id nothing knows.
    _persist_batch(state)
    thread = threading.Thread(
        target=_execute_batch, args=(batch_id, graph, records, workers), daemon=True
    )
    with _lock:
        _threads[batch_id] = thread
    thread.start()
    return batch_id


def wait_for(batch_id: str, timeout: float | None = None) -> bool:
    """Block until a batch's worker thread has finished and persisted it."""
    with _lock:
        thread = _threads.get(batch_id)
    if thread is None:
        return True
    thread.join(timeout)
    return not thread.is_alive()


def _execute_batch(batch_id: str, graph: dict, records: list[dict], workers: int) -> None:
    state = _batches[batch_id]

    def work(item: dict, record: dict) -> None:
        with _lock:
            # The pool hands out work one item at a time, so a cancelled batch
            # stops at the next item rather than at the next record read: at
            # most `workers` items are already in flight and have to finish.
            if state.get("cancel_requested"):
                item["status"] = "cancelled"
                state["cancelled_items"] += 1
                return
            item["status"] = "running"
            # pre-assign the run id so live node progress is visible while
            # this item executes (start_run runs synchronously here)
            item["run_id"] = uuid.uuid4().hex[:12]
        try:
            runner.start_run(graph, record, background=False,
                             gray_zone=state.get("gray_zone"),
                             run_id=item["run_id"])
            run = runner.get_run(item["run_id"]) or {}
            with _lock:
                item["status"] = run.get("status", "failed")
                item["error"] = run.get("error")
                item["review_status"] = run.get("review_status")
                if run.get("status") == "success":
                    item["output_summary"] = _summarize(
                        json.dumps(run.get("result"), ensure_ascii=False, default=str)
                    )
            if state.get("metric") and run.get("status") == "success":
                _score_item(state, item, run.get("result"))
        except Exception as e:
            with _lock:
                item["status"] = "failed"
                item["error"] = str(e)

    def run_group(group):
        for item, record in group:
            work(item, record)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(run_group, _grouped(zip(state["items"], records))))
    if state.get("metric"):
        from . import evaluation
        with _lock:
            # Scored over what actually ran. A cancelled batch still reports a
            # mean, and `scored`/`total` show how much of the set it covers.
            state["summary"] = evaluation.summarise(state["items"])
    state["status"] = "cancelled" if state.get("cancel_requested") else "completed"
    _persist_batch(state)


def cancel_batch(batch_id: str) -> dict:
    """Stop a batch from starting any more items.

    Items already executing cannot be interrupted — each is a `WorkFlow.execute`
    call inside the framework — so they run to completion and are kept. What
    this saves is everything not yet started, which for a long evaluation is
    nearly all of it.
    """
    with _lock:
        state = _batches.get(batch_id)
        if state is None:
            persisted = _read_batch(batch_id)
            if persisted is None:
                return {"cancelled": False, "reason": "no such batch"}
            return {"cancelled": False,
                    "reason": f"batch already {persisted.get('status')}"}
        if state.get("cancel_requested"):
            # Already winding down: report that plainly rather than as a
            # refusal, so a second click is not an error.
            return {"cancelled": True, "already_requested": True,
                    **_cancel_counts(state)}
        if state.get("status") != "running":
            return {"cancelled": False, "reason": f"batch already {state.get('status')}"}
        state["cancel_requested"] = True
        state["status"] = "cancelling"
        counts = _cancel_counts(state)
        snapshot = dict(state)
    _persist_batch(snapshot)
    return {"cancelled": True, **counts}


def _cancel_counts(state: dict) -> dict:
    """What cancelling this batch actually saves, and what it cannot."""
    items = state.get("items") or []
    return {
        "not_started": sum(1 for i in items if i.get("status") == "pending"),
        "still_running": sum(1 for i in items if i.get("status") == "running"),
        "finished": sum(1 for i in items
                        if i.get("status") not in ("pending", "running")),
    }


def _grouped(pairs) -> list[list]:
    """Records that must not overtake each other, kept together.

    Stepping a window turns one sample into a sequence: each record carries the
    news up to its own date, and each run's memory is what the next one reads.
    Run them at the same time and a later step's judgement can land before an
    earlier one's. Different samples are independent and still run in parallel.
    """
    groups: dict[str, list] = {}
    loose: list[list] = []
    for item, record in pairs:
        key = (record or {}).get("sample_id")
        if key and (record or {}).get("as_of"):
            groups.setdefault(str(key), []).append((item, record))
        else:
            loose.append([(item, record)])
    return list(groups.values()) + loose


def _score_item(state: dict, item: dict, prediction) -> None:
    """Score one finished item.

    A metric that raises marks that item unscored and says why, rather than
    failing the batch: the runs themselves succeeded and are worth keeping,
    and the aggregate reports how many went unscored.
    """
    from . import evaluation
    try:
        scored = evaluation.score_one(state["metric"], prediction, item.get("label"))
    except Exception as e:
        with _lock:
            item["score_detail"] = {"error": str(e)}
        return
    with _lock:
        item["score"] = scored.get("score")
        extra = {k: v for k, v in scored.items() if k != "score"}
        item["score_detail"] = extra or None


def mark_interrupted() -> int:
    """Flag persisted batches left mid-flight by a previous process.

    Like runs, a batch cannot be resumed: each item was a run in a worker
    thread. Items that had already finished keep their status and score, so an
    interrupted batch still shows what it managed to complete.
    """
    if not BATCHES_DIR.is_dir():
        return 0
    marked = 0
    for path in BATCHES_DIR.glob("*.json"):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("status") != "running":
            continue
        data["status"] = "interrupted"
        for item in data.get("items") or []:
            if item.get("status") in ("running", "pending"):
                item["status"] = "failed"
                item["error"] = "Interrupted by a server restart."
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False, default=str)
            marked += 1
        except OSError:
            continue
    return marked


def _persist_batch(state: dict) -> None:
    BATCHES_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(BATCHES_DIR / f"{state['batch_id']}.json", "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False, default=str)
    except OSError:
        pass


def apply_review_outcome(run_id: str, review: dict) -> None:
    """Update in-memory batch items for a resolved review's run."""
    with _lock:
        for state in _batches.values():
            for item in state.get("items", []):
                if item.get("run_id") == run_id:
                    item["review_status"] = review["status"]
                    item["final_action"] = review["final_action"]


def _node_names(state: dict) -> list[str]:
    """Node names for progress aggregation: live runs' node lists first
    (source nodes included), falling back to the graph document."""
    names: list[str] = []
    seen = set()
    for item in state.get("items", []):
        run_id = item.get("run_id")
        if not run_id:
            continue
        run = runner.get_run(run_id)
        if not run:
            continue
        for node in run.get("nodes", []) or []:
            if node.get("name") not in seen:
                seen.add(node.get("name"))
                names.append(node.get("name"))
    if names:
        return names
    from . import graphs as graph_store  # lazy: graphs has no dependency on batch

    graph = graph_store.load_graph(state.get("graph_id") or "") or {}
    return [t.get("name") for t in graph.get("tasks", []) or []]


def _node_progress(state: dict) -> dict:
    """Aggregate per-node status counts across the batch's item runs.

    {node_name: {completed, running, failed, pending}}; pending items (or
    items whose run state is unavailable) count all nodes as pending.
    """
    progress = {
        name: {"completed": 0, "running": 0, "failed": 0, "pending": 0}
        for name in _node_names(state)
    }
    for item in state.get("items", []):
        run = runner.get_run(item["run_id"]) if item.get("run_id") else None
        if run is None:
            for name in progress:
                progress[name]["pending"] += 1
            continue
        by_name = {n.get("name"): n.get("status", "pending")
                   for n in run.get("nodes", []) or []}
        for name in progress:
            status = by_name.get(name, "pending")
            bucket = status if status in progress[name] else "pending"
            progress[name][bucket] += 1
    return progress


def _digest(state: dict) -> dict:
    """One batch, small enough to list.

    The items are the bulk of a batch document — hundreds of records with their
    outputs — and a history listing needs none of them, only the shape of the
    result.
    """
    items = state.get("items") or []
    counts: dict[str, int] = {}
    for item in items:
        status = item.get("status") or "pending"
        counts[status] = counts.get(status, 0) + 1
    return {
        "batch_id": state.get("batch_id"),
        "graph_id": state.get("graph_id"),
        "status": state.get("status"),
        "created_at": state.get("created_at"),
        "source": state.get("source"),
        "metric": state.get("metric"),
        "summary": state.get("summary"),
        "total": state.get("total", len(items)),
        "counts": counts,
    }


def list_batches(graph_id: str | None = None, limit: int = 50) -> list[dict]:
    """Recent batches (newest first), merging in-memory and persisted state.

    Persisted batches outlive the process that ran them, so a finished
    evaluation stays reachable after a restart — which is the point of keeping
    them on disk at all.
    """
    by_id: dict[str, dict] = {}
    if BATCHES_DIR.is_dir():
        for path in BATCHES_DIR.glob("*.json"):
            state = _read_batch(path.stem)
            if state is not None:
                by_id[path.stem] = _digest(state)
    with _lock:
        for batch_id, state in _batches.items():
            by_id[batch_id] = _digest(state)
    batches = list(by_id.values())
    if graph_id:
        batches = [b for b in batches if b.get("graph_id") == graph_id]
    batches.sort(key=lambda b: b.get("created_at") or "", reverse=True)
    return batches[:limit]


def _read_batch(batch_id: str) -> dict | None:
    """A batch as persisted on disk, or None."""
    path = BATCHES_DIR / f"{batch_id}.json"
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def get_batch(batch_id: str) -> dict | None:
    with _lock:
        state = _batches.get(batch_id)
        if state is not None:
            return {**state, "node_progress": _node_progress(state)}
    state = _read_batch(batch_id)
    if state is None:
        return None
    return {**state, "node_progress": _node_progress(state)}
