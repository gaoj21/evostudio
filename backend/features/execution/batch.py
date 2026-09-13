"""Batch run engine for EvoAgentX Studio.

A batch runs a list of input records concurrently (ThreadPoolExecutor,
workers configurable 1-5); each record is a normal run via
runner.start_run in synchronous mode. State lives in a module-level dict
and is persisted to backend/data/batches/{batch_id}.json on completion.
"""

import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from backend.api import runner
from backend.api.studio_config import data_path

BATCHES_DIR = data_path("batches")

# Not a throttle, a sanity bound: the provider's concurrency limit is in the
# thousands, and the useful number of workers is the number of samples
# (their steps run one after another). Sixty-four covers any split here.
MAX_WORKERS = 64

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
    from backend.features.execution import provider_batch
    size = provider_batch.validate(graph, graph.get('_llm_batch_size'))
    batch_id = uuid.uuid4().hex[:12]
    if size is not None:
        workers = min(size, MAX_WORKERS)
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
        "llm_batch_size": size,
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
        target=_execute_batch,
        args=(batch_id, graph, list(zip(state["items"], records)), workers), daemon=True
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


# A record whose run could not reach the model is run again, after a pause,
# once the rest of the batch has had its turn: an outage that outlasts one
# run's waiting window rarely outlasts the batch. Two more passes, then it
# stays failed and the item says the model was unreachable.
RETRY_PASSES = 2
RETRY_PAUSE_SECONDS = 60.0
_pause = time.sleep


def _retryable(item: dict, run: dict | None) -> bool:
    node_error = (run or {}).get("node_error") or {}
    return item.get("status") == "failed" and bool(node_error.get("retryable"))


def _execute_batch(batch_id: str, graph: dict, pairs: list, workers: int) -> None:
    """Run `pairs` — (item, record) — of the batch; the rest of its items are
    left as they are. A fresh batch passes all of them; a resumed one only
    what did not finish."""
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
                             run_id=item["run_id"], batch_id=batch_id,
                             session_started_at=state.get("created_at"),
                             **({"llm_batch_size": state["llm_batch_size"]} if state.get("llm_batch_size") else {}))
            run = runner.get_run(item["run_id"]) or {}
            with _lock:
                item["status"] = run.get("status", "failed")
                item["error"] = run.get("error")
                item["review_status"] = run.get("review_status")
                item["attempts"] = item.get("attempts", 0) + 1
                item["retryable"] = _retryable(item, run)
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
        """The steps of one sample, in order. A step that does not succeed
        stops the rest: the next step would read this one's memory, and
        a judgement made without it is not the judgement the week asks for."""
        blocked_by = None
        for item, record in group:
            if blocked_by is not None and (record or {}).get("as_of"):
                with _lock:
                    item["status"] = "blocked"
                    item["error"] = (f"Not run: the step before it ({blocked_by}) did not finish. "
                                     f"Resume runs them in order.")
                continue
            work(item, record)
            if (record or {}).get("as_of") and item.get("status") != "success":
                blocked_by = record.get("as_of")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(run_group, _grouped(pairs)))

    for _ in range(RETRY_PASSES):
        if state.get("cancel_requested"):
            break
        again = _with_blocked_after(pairs, [(item, record) for item, record in pairs
                                            if item.get("retryable")])
        if not again:
            break
        with _lock:
            state["retry_note"] = (f"{len(again)} record(s) could not reach the model; "
                                   f"trying again after {RETRY_PAUSE_SECONDS:.0f}s.")
            for item, _rec in again:
                item["status"] = "pending"
                item["error"] = None
                item["retryable"] = False
        _persist_batch(state)
        _pause(RETRY_PAUSE_SECONDS)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(run_group, _grouped(again)))
    with _lock:
        state.pop("retry_note", None)
    if state.get("metric"):
        from backend.api import evaluation
        with _lock:
            # Scored over what actually ran. A cancelled batch still reports a
            # mean, and `scored`/`total` show how much of the set it covers.
            state["summary"] = evaluation.summarise(state["items"])
    state["status"] = _outcome(state)
    _persist_batch(state)


def _outcome(state: dict) -> str:
    """What the batch's outcome is called.

    `completed` used to cover everything from all-green to every record
    dead, and the badge painted it green either way. A batch that stopped
    is `cancelled`; one with any failed record is `completed_with_errors`;
    one where nothing succeeded is `failed`; only all-green is `succeeded`.
    """
    if state.get("cancel_requested"):
        return "cancelled"
    statuses = [i.get("status") for i in state.get("items") or []]
    if any(s in ("failed", "blocked") for s in statuses):
        return "completed_with_errors" if any(s == "success" for s in statuses) else "failed"
    return "succeeded"


def cancel_batch(batch_id: str) -> dict:
    """Stop a batch: start no more items, and interrupt the ones running.

    Each record runs on the engine's own loop, so a stop reaches into the
    model call it is inside; nothing finishes on the user's money after
    they said stop. What was already complete is kept.
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
        if state.get("status") not in ("running", "cancelling"):
            return {"cancelled": False, "reason": f"batch already {state.get('status')}"}
        state["cancel_requested"] = True
        state["status"] = "cancelling"
        counts = _cancel_counts(state)
        in_flight = [i.get("run_id") for i in state.get("items") or []
                     if i.get("status") == "running" and i.get("run_id")]
        snapshot = dict(state)
    for run_id in in_flight:
        runner.cancel_run(run_id)
    _persist_batch(snapshot)
    return {"cancelled": True, **counts}


RESUMABLE = ("cancelled", "interrupted", "completed_with_errors", "failed", "completed")


def _group_key(record: dict) -> str | None:
    """Stepped records of one sample share a key; anything else stands alone."""
    record = record or {}
    if record.get("sample_id") and record.get("as_of"):
        return str(record["sample_id"])
    return None


def _with_blocked_after(pairs: list, chosen: list) -> list:
    """`chosen` plus, for each stepped sample, every later step of that
    sample — blocked ones and finished ones alike — in the batch's order.

    A step that runs again writes new memory; the steps after it read that
    memory, so their earlier results no longer describe this run. Standalone
    records bring nothing along."""
    picked = {id(item) for item, _ in chosen}
    open_groups: dict[str, bool] = {}
    out = []
    for item, record in pairs:
        key = _group_key(record)
        if id(item) in picked:
            out.append((item, record))
            if key:
                open_groups[key] = True
        elif key and open_groups.get(key):
            out.append((item, record))
    return out


def unfinished(state: dict) -> list[dict]:
    """The items a resume would run: everything that did not succeed, and —
    for a stepped sample — everything after its first such step."""
    pairs = [(i, i.get("inputs") or {}) for i in (state.get("items") or [])]
    first = [(i, r) for i, r in pairs if i.get("status") != "success"]
    return [i for i, _ in _with_blocked_after(pairs, first)]


def resume_batch(batch_id: str, graph: dict) -> dict:
    """Pick a stopped batch up where it left off.

    The records that succeeded are kept, with their scores; the ones that
    were cancelled, interrupted by a restart, or failed are run again, in
    their original order, on the workflow as it is now and the memory as it
    is now. A stepped sample's remaining steps therefore read what its
    earlier steps wrote — which is why the memory must not be reset between
    stopping and resuming.
    """
    with _lock:
        state = _batches.get(batch_id)
        if state is None:
            state = _read_batch(batch_id)
            if state is None:
                return {"resumed": False, "reason": "no such batch"}
        if state.get("status") in ("running", "cancelling"):
            return {"resumed": False, "reason": "batch is still running"}
        if state.get("graph_id") and graph.get("id") and state["graph_id"] != graph.get("id"):
            return {"resumed": False, "reason": "batch belongs to another workflow"}
        todo = unfinished(state)
        if not todo:
            return {"resumed": False, "reason": "nothing left to run"}
        kept = len(state.get("items") or []) - len(todo)
        # Finished steps that come after a step being run again: their
        # result was made without it, so they run again too.
        rerun = sum(1 for i in todo if i.get("status") == "success")
        for item in todo:
            item.update({"status": "pending", "run_id": None, "error": None,
                         "output_summary": None, "review_status": None,
                         "retryable": False, "score": None, "score_detail": None})
        state["status"] = "running"
        state["cancel_requested"] = False
        state["cancelled_items"] = 0
        state.setdefault("resumed_at", []).append(_utcnow())
        state["summary"] = None
        _batches[batch_id] = state
        pairs = [(item, item.get("inputs") or {}) for item in todo]
        workers = max(1, min(int(state.get("workers") or 2), MAX_WORKERS))
    _persist_batch(state)
    thread = threading.Thread(
        target=_execute_batch, args=(batch_id, graph, pairs, workers), daemon=True)
    with _lock:
        _threads[batch_id] = thread
    thread.start()
    return {"resumed": True, "batch_id": batch_id, "remaining": len(todo), "kept": kept,
            "rerun_after": rerun}


def _cancel_counts(state: dict) -> dict:
    """What cancelling this batch actually saves, and what it cannot."""
    items = state.get("items") or []
    return {
        "not_started": sum(1 for i in items if i.get("status") == "pending"),
        # Being stopped where they are, not left to finish.
        "interrupted": sum(1 for i in items if i.get("status") == "running"),
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
    from backend.api import evaluation
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
    from backend.api import graphs as graph_store  # lazy: graphs has no dependency on batch

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


def set_evaluation(batch_id: str, report: dict) -> dict | None:
    """Attach a report to a batch, live or on disk, and persist it."""
    with _lock:
        state = _batches.get(batch_id)
    if state is None:
        state = _read_batch(batch_id)
        if state is None:
            return None
    with _lock:
        state["evaluation"] = report
        snapshot = dict(state)
    _persist_batch(snapshot)
    return state


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
        # The one line an evaluation boils down to, for the history list.
        "evaluation": (state.get("evaluation") or {}).get("headline"),
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
