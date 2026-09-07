"""HITL web review for EvoAgentX Studio.

After a successful run, node outputs are scanned for a decision object
({"action": "alert"|"suppress", "score": 0-100, ...}). An alert whose score
lands in the gray zone (default 35-65, the WoE band scaled to the rubric's
0-100, overridable per run) — or any decision carrying "review_required":
true — is routed to human review: a pending record is persisted to
studio/data/reviews/<review_id>.json and the run is marked
review_status="awaiting_review". Resolving via POST /api/review/{id} writes
the outcome back to the run (and the batch item, if any).

This deliberately does NOT use evoagentx.hitl.HITLManager (tkinter); it is a
pure web/polling flow.
"""

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

REVIEWS_DIR = _REPO_ROOT / "studio" / "data" / "reviews"
RUNS_DIR = _REPO_ROOT / "studio" / "data" / "runs"
BATCHES_DIR = _REPO_ROOT / "studio" / "data" / "batches"

DEFAULT_GRAY_ZONE = (35, 65)

_lock = threading.Lock()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_json(text):
    from evoagentx.core.module_utils import parse_json_from_llm_output

    try:
        return parse_json_from_llm_output(str(text))
    except Exception:
        return None


def _find_decision(nodes: list[dict]) -> dict | None:
    """First node output value that parses to a decision-like dict."""
    for node in nodes or []:
        for value in (node.get("output") or {}).values():
            parsed = _parse_json(value)
            if isinstance(parsed, dict) and parsed.get("action") in ("alert", "suppress"):
                return parsed
    return None


def _find_detection(nodes: list[dict]) -> dict:
    for node in nodes or []:
        for value in (node.get("output") or {}).values():
            parsed = _parse_json(value)
            if isinstance(parsed, dict) and ("topic" in parsed or "severity" in parsed):
                return parsed
    return {}


def maybe_create_review(run_state: dict, gray_zone=None) -> dict | None:
    """Route a successful run's decision to review when it lands in the gray
    zone (or carries review_required). Returns the review record or None."""
    decision = _find_decision(run_state.get("nodes"))
    if not decision:
        return None
    lo, hi = gray_zone or DEFAULT_GRAY_ZONE
    try:
        score = float(decision.get("score"))
    except (TypeError, ValueError):
        score = None
    needs_review = bool(decision.get("review_required")) or (
        decision.get("action") == "alert" and score is not None and lo <= score <= hi
    )
    if not needs_review:
        return None

    detection = _find_detection(run_state.get("nodes"))
    inputs = run_state.get("inputs") or {}
    review = {
        "review_id": uuid.uuid4().hex[:12],
        "run_id": run_state.get("run_id"),
        "graph_id": run_state.get("graph_id"),
        "status": "pending",
        "company": inputs.get("company", ""),
        "topic": detection.get("topic", ""),
        "severity": detection.get("severity", ""),
        "score": score,
        "risk_level": decision.get("risk_level", ""),
        "summary": detection.get("summary", ""),
        "decision": decision,
        "zone": [lo, hi],
        "created_at": _utcnow(),
        "resolved_at": None,
        "resolution": None,
        "note": None,
    }
    with _lock:
        REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
        _save(review)
    return review


def _save(review: dict) -> None:
    with open(REVIEWS_DIR / f"{review['review_id']}.json", "w", encoding="utf-8") as f:
        json.dump(review, f, indent=2, ensure_ascii=False, default=str)


def get_review(review_id: str) -> dict | None:
    path = REVIEWS_DIR / f"{review_id}.json"
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def list_reviews(status: str | None = None) -> list[dict]:
    reviews = []
    if REVIEWS_DIR.is_dir():
        for path in REVIEWS_DIR.glob("*.json"):
            try:
                with open(path, encoding="utf-8") as f:
                    reviews.append(json.load(f))
            except (json.JSONDecodeError, OSError):
                continue
    if status:
        reviews = [r for r in reviews if r.get("status") == status]
    reviews.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return reviews[:50]


def resolve_review(review_id: str, decision: str, note: str | None = None) -> dict | None:
    """decision: "approve" (final action = alert) or "reject" (= suppress)."""
    if decision not in ("approve", "reject"):
        raise ValueError("decision must be 'approve' or 'reject'")
    with _lock:
        review = get_review(review_id)
        if review is None:
            return None
        if review.get("status") != "pending":
            raise ValueError(f"Review '{review_id}' is already {review.get('status')}")
        review["status"] = "approved" if decision == "approve" else "rejected"
        review["resolution"] = decision
        review["final_action"] = "alert" if decision == "approve" else "suppress"
        review["note"] = note or ""
        review["resolved_at"] = _utcnow()
        _save(review)
    _writeback(review)
    return review


def _writeback(review: dict) -> None:
    """Record the review outcome on the run JSON and the batch item, if any."""
    run_id = review.get("run_id")
    outcome = {
        "review_id": review["review_id"],
        "review_status": review["status"],
        "final_action": review["final_action"],
        "review_note": review.get("note") or "",
    }
    import batch as batch_mod  # lazy: batch imports runner, runner imports review
    import runner as runner_mod  # lazy: runner imports this module

    runner_mod.apply_review_outcome(run_id, outcome)
    batch_mod.apply_review_outcome(run_id, review)
    run_path = RUNS_DIR / f"{run_id}.json"
    if run_path.is_file():
        try:
            with open(run_path, encoding="utf-8") as f:
                run = json.load(f)
            run.update(outcome)
            with open(run_path, "w", encoding="utf-8") as f:
                json.dump(run, f, indent=2, ensure_ascii=False, default=str)
        except (json.JSONDecodeError, OSError):
            pass
    if not BATCHES_DIR.is_dir():
        return
    for path in BATCHES_DIR.glob("*.json"):
        try:
            with open(path, encoding="utf-8") as f:
                batch = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        changed = False
        for item in batch.get("items", []):
            if item.get("run_id") == run_id:
                item["review_status"] = review["status"]
                item["final_action"] = review["final_action"]
                changed = True
        if changed:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(batch, f, indent=2, ensure_ascii=False, default=str)
            except OSError:
                pass
