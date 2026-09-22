"""Human review of run outputs for EvoAgentX Studio.

A workflow decides which outputs a person should check with a review rule,
saved on the graph as `review`:

    {"node": "decide",            # node whose output is checked (default: any)
     "score_field": "score",      # numeric field compared to `range`
     "range": [35, 65],           # inclusive band that needs a person
     "when": {"field": "action", "equals": "alert"},   # optional precondition
     "flag_field": "review_required",   # a true value always needs a person
     "show": ["action", "rationale"],   # fields shown to the reviewer
     "approve_label": "alert", "reject_label": "suppress"}  # final outcome names

Without a rule only an output carrying `review_required: true` is routed.
A batch may pass its own band (review_zone), which replaces `range`.
Matching runs get a pending record in studio-data/reviews/<review_id>.json
and review_status="awaiting_review"; resolving it via POST /api/review/{id}
writes the outcome back to the run and its batch item.

This deliberately does NOT use evoagentx.hitl.HITLManager (tkinter); it is a
pure web/polling flow.
"""

import json
import threading
import uuid
from datetime import datetime, timezone
from backend.api.studio_config import data_path

REVIEWS_DIR = data_path("reviews")
RUNS_DIR = data_path("runs")
BATCHES_DIR = data_path("batches")

DEFAULT_FLAG_FIELD = "review_required"
MAX_SHOWN = 12

_lock = threading.Lock()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_rule(rule) -> dict:
    """A review rule as saved on a graph; raises ValueError for a bad one."""
    if not isinstance(rule, dict):
        raise ValueError("Review settings must be an object.")
    out = {}
    for key in ("node", "score_field", "flag_field", "approve_label", "reject_label"):
        value = rule.get(key)
        if value in (None, ""):
            continue
        if not isinstance(value, str) or len(value) > 120:
            raise ValueError(f"Review '{key}' must be a short name.")
        out[key] = value
    if rule.get("range") is not None:
        out["range"] = list(_band(rule["range"]))
    if rule.get("when"):
        when = rule["when"]
        if not isinstance(when, dict) or not isinstance(when.get("field"), str) or "equals" not in when:
            raise ValueError("Review 'when' needs a field and the value it must equal.")
        out["when"] = {"field": when["field"], "equals": when["equals"]}
    show = rule.get("show") or []
    if not isinstance(show, list) or len(show) > MAX_SHOWN or any(not isinstance(s, str) for s in show):
        raise ValueError(f"Review 'show' lists at most {MAX_SHOWN} field names.")
    if show:
        out["show"] = show
    if "range" in out and not out.get("score_field"):
        out["score_field"] = "score"
    return out


def _band(value) -> tuple:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError("Review range must be [low, high].")
    try:
        lo, hi = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        raise ValueError("Review range must be two numbers.") from None
    if lo > hi:
        raise ValueError("Review range needs low <= high.")
    return lo, hi


def _parse_json(text):
    if isinstance(text, dict):
        return text
    from evoagentx.core.module_utils import parse_json_from_llm_output

    try:
        return parse_json_from_llm_output(str(text))
    except Exception:
        return None


def _outputs(run_state: dict, node: str | None):
    """(node name, parsed output object) pairs, full values — never the
    display copies, which are clipped and no longer parse."""
    full = run_state.get("node_outputs") or {}
    if not full:
        full = {n.get("name"): n.get("output") for n in run_state.get("nodes") or []}
    for name, outputs in full.items():
        if node and name != node:
            continue
        values = outputs.values() if isinstance(outputs, dict) else [outputs]
        for value in values:
            parsed = _parse_json(value)
            if isinstance(parsed, dict):
                yield name, parsed


def _matches(obj: dict, rule: dict, band) -> tuple[bool, float | None]:
    flag = rule.get("flag_field") or DEFAULT_FLAG_FIELD
    try:
        score = float(obj.get(rule["score_field"])) if rule.get("score_field") in obj else None
    except (TypeError, ValueError):
        score = None
    if obj.get(flag) is True:
        return True, score
    if band is None or score is None:
        return False, score
    when = rule.get("when")
    if when and obj.get(when["field"]) != when["equals"]:
        return False, score
    return band[0] <= score <= band[1], score


def maybe_create_review(run_state: dict, gray_zone=None, rule: dict | None = None) -> dict | None:
    """Route a successful run to review when an output matches the rule.
    Returns the review record or None."""
    rule = dict(rule or {})
    if gray_zone:
        rule.setdefault("score_field", "score")
        band = _band(gray_zone)
    else:
        band = tuple(rule["range"]) if rule.get("range") else None
    match = None
    for name, obj in _outputs(run_state, rule.get("node")):
        hit, score = _matches(obj, rule, band)
        if hit:
            match = (name, obj, score)
            break
    if match is None:
        return None
    name, obj, score = match
    shown = rule.get("show") or [k for k in obj if not isinstance(obj[k], (dict, list))][:MAX_SHOWN]
    review = {
        "review_id": uuid.uuid4().hex[:12],
        "run_id": run_state.get("run_id"),
        "graph_id": run_state.get("graph_id"),
        "status": "pending",
        "node": name,
        "score": score,
        "fields": {k: obj.get(k) for k in shown if k in obj},
        "output": obj,
        "zone": list(band) if band else None,
        "approve_label": rule.get("approve_label") or "approved",
        "reject_label": rule.get("reject_label") or "rejected",
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
    """decision: "approve" or "reject"; the final outcome is the rule's label
    for it (e.g. "alert" / "suppress"), or approved / rejected."""
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
        review["final_action"] = (review.get("approve_label") or "approved") if decision == "approve" \
            else (review.get("reject_label") or "rejected")
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
    # Lazy imports break the batch -> runner -> review cycle.
    from backend.api import batch as batch_mod
    from backend.api import runner as runner_mod

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
