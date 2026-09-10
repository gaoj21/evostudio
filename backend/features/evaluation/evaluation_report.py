"""Evaluate a batch after it ran, from what it already holds.

Scoring used to be something you had to decide before pressing Run: pick a
metric, type the name of the label field, and only then did a batch carry a
score. Most batches were started without one, and the questions that
mattered — which companies did it catch, how early, which did it cry wolf
on — were answered by hand from the records.

A report is computed from a finished (or half-finished) batch and its runs:

- **credit-risk mode**, when the records came from the credit-risk feed
  (they carry `sample_json`, so the truth travels with the data): per
  company, the steps in order with action / level / score, whether it was
  detected, how many days ahead, and how its weekly level compares with
  explicitly reviewed evidence-based labels; then the tallies, with and without the
  samples the dataset itself flags as noise.
- **metric mode**, for any other batch: a metric and the field holding the
  expected answer, applied to each record's result; the per-record scores
  and the summary the batch would have carried had it been started as an
  evaluation.
"""

import json
from datetime import date
from statistics import fmean

from backend.api import evaluation

BANDS = ("low", "medium", "high", "critical")
ALERT_LEVELS = ("high", "critical")


def _sample(item: dict) -> dict | None:
    raw = (item.get("inputs") or {}).get("sample_json")
    if not raw:
        return None
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None


def is_credit_risk(batch: dict) -> bool:
    return any(_sample(i) for i in (batch.get("items") or [])[:5])


def _decision(result) -> dict | None:
    """The decide node's JSON, from a run result of either shape."""
    if result is None:
        return None
    value = result.get("decision") if isinstance(result, dict) else result
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            # A stray quote or comma from the model should not turn a step
            # into "no level": read the three fields straight off the text.
            return _decision_by_eye(value)
    return value if isinstance(value, dict) else None


def _decision_by_eye(text: str) -> dict | None:
    import re
    out = {}
    for key in ("action", "risk_level", "rationale"):
        m = re.search(rf'"{key}"\s*:\s*"([^"]*)"', text)
        if m:
            out[key] = m.group(1)
    m = re.search(r'"score"\s*:\s*(\d+(?:\.\d+)?)', text)
    if m:
        out["score"] = float(m.group(1)) if "." in m.group(1) else int(m.group(1))
    return out or None


def expected_band(sample: dict, as_of: str) -> str | None:
    """Only an explicitly reviewed, evidence-based label for this exact day.

    An eventual insolvency does not determine the risk visible beforehand;
    absence of a recorded insolvency does not establish low risk.
    """
    annotation = (sample.get("risk_annotations") or {}).get(as_of)
    if not isinstance(annotation, dict):
        return None
    if annotation.get("review_status") != "reviewed" or not annotation.get("evidence_ids"):
        return None
    level = annotation.get("risk_level")
    return level if level in BANDS else None


def countdown_band(company: dict, as_of: str) -> str | None:
    """The band the countdown to a verified insolvency implies (≤30 days
    critical, ≤90 high, ≤180 medium). Not ground truth for the day — a
    company can be quiet a month before filing — so it is reported beside
    the reviewed label, never in its place, and only for positives."""
    if company.get("type") != "positive" or not company.get("event_date") or not as_of:
        return None
    try:
        days = (date.fromisoformat(company["event_date"]) - date.fromisoformat(as_of)).days
    except ValueError:
        return None
    return "critical" if days <= 30 else "high" if days <= 90 else "medium"


def _band_index(level) -> int | None:
    try:
        return BANDS.index(str(level).lower())
    except ValueError:
        return None


_MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"])}


def future_mentions(text: str, as_of: str) -> list[str]:
    """Dates named in `text` that lie after `as_of`: ISO dates, and
    "March 2025"-style month names. A model that writes "filed in March
    2025" at a step dated October 2024 is not reading the material."""
    import re
    if not as_of or not text:
        return []
    found = [m.group(0) for m in re.finditer(r"\b(20\d\d)-(\d\d)-(\d\d)\b", text) if m.group(0) > as_of]
    for m in re.finditer(r"\b(January|February|March|April|May|June|July|August|September|"
                         r"October|November|December)\s+(20\d\d)\b", text):
        first = f"{m.group(2)}-{_MONTHS[m.group(1).lower()]:02d}-01"
        if first > as_of:
            found.append(m.group(0))
    return sorted(set(found))


def credit_risk_report(batch: dict, result_of, text_of=None, label_of=None) -> dict:
    """`result_of(item)` returns the run result of one item, or None;
    `text_of(item)`, when given, returns what the run's nodes wrote, for the
    foresight check; `label_of(sample_id)`, when given, supplies the truth
    for records whose sample carries none (the versioned releases keep
    outcomes out of the inputs on purpose)."""
    companies: dict[str, dict] = {}
    for item in batch.get("items") or []:
        sample = _sample(item)
        if sample is None:
            continue
        inputs = item.get("inputs") or {}
        key = sample.get("sample_id") or inputs.get("sample_id") or inputs.get("company")
        if key not in companies:
            label = sample.get("label") or {}
            kind = sample.get("type")
            reason = None
            if kind is None and label_of is not None:
                resolved = label_of(key) or {"type": "unverified", "reason": "no outcome record"}
                kind = resolved.get("type")
                reason = resolved.get("reason")
                label = {**label, **{k: v for k, v in resolved.items() if k in ("event_date", "event")}}
            elif kind is None:
                kind, reason = "unverified", "the records carry no outcome and no dataset was named"
            companies[key] = {
                "sample_id": key,
                "company": inputs.get("company") or (sample.get("company") or {}).get("name"),
                "type": kind,
                "unverified_reason": reason,
                "event_date": label.get("event_date"),
                "flags": list((sample.get("news_quality") or {}).get("flags") or []),
                "negative_verified": label.get("negative_review_status") == "verified",
                "steps": [],
            }
        company = companies[key]
        decision = _decision(result_of(item)) if item.get("status") == "success" else None
        level = (decision or {}).get("risk_level")
        as_of = inputs.get("as_of") or inputs.get("window_end")
        expected = expected_band(sample, as_of)
        got, want = _band_index(level), _band_index(expected)
        countdown = countdown_band(company, as_of)
        cd = _band_index(countdown)
        foresight = future_mentions(text_of(item), as_of) if text_of and item.get("status") == "success" else []
        company["steps"].append({
            "foresight": foresight,
            "countdown": countdown,
            "countdown_match": (None if got is None or cd is None else "exact" if got == cd
                                else "near" if abs(got - cd) == 1 else "off"),
            "as_of": as_of,
            "status": item.get("status"),
            "action": (decision or {}).get("action"),
            "risk_level": level,
            "score": (decision or {}).get("score"),
            "expected": expected,
            "match": (None if got is None or want is None else "exact" if got == want
                      else "near" if abs(got - want) == 1 else "off"),
            "rationale": (decision or {}).get("rationale"),
        })

    rows = []
    for company in companies.values():
        steps = sorted(company["steps"], key=lambda s: str(s["as_of"] or ""))
        alerts = [s for s in steps if s["risk_level"] in ALERT_LEVELS]
        first = alerts[0]["as_of"] if alerts else None
        lead = None
        if first and company["event_date"]:
            lead = (date.fromisoformat(company["event_date"]) - date.fromisoformat(first)).days
        judged = [s for s in steps if s["match"] is not None]
        cd_judged = [s for s in steps if s.get("countdown_match") is not None]
        rows.append({
            "countdown_weeks": len(cd_judged),
            "countdown_near": sum(1 for s in cd_judged if s["countdown_match"] in ("exact", "near")),
            **company, "steps": steps,
            "detected": bool(alerts) if company["type"] == "positive" else None,
            "false_alarm": bool(alerts) if company["type"] == "negative" and company["negative_verified"] else None,
            "first_alert": first,
            "lead_days": lead,
            "alerts_raised": sum(1 for s in steps if s["action"] == "alert"),
            "weeks": len(judged),
            "weeks_exact": sum(1 for s in judged if s["match"] == "exact"),
            "weeks_near": sum(1 for s in judged if s["match"] in ("exact", "near")),
            "failed_steps": sum(1 for s in steps if s["status"] != "success"),
        })
    order = {"positive": 0, "negative": 1}
    rows.sort(key=lambda r: (order.get(r["type"], 2), str(r["company"] or "")))

    def tally(subset: list[dict]) -> dict:
        pos = [r for r in subset if r["type"] == "positive"]
        neg = [r for r in subset if r["type"] == "negative" and r["negative_verified"]]
        leads = [r["lead_days"] for r in pos if r["lead_days"] is not None]
        weeks = sum(r["weeks"] for r in subset)
        cd_weeks = sum(r["countdown_weeks"] for r in pos)
        return {
            "positives": len(pos),
            "detected": sum(1 for r in pos if r["detected"]),
            "negatives": len(neg),
            "unverified_negatives": sum(r["type"] == "negative" and not r["negative_verified"] for r in subset),
            "unverified": sum(1 for r in subset if r["type"] not in ("positive", "negative")),
            "false_alarms": sum(1 for r in neg if r["false_alarm"]),
            "mean_lead_days": round(fmean(leads)) if leads else None,
            # Positives' weekly level against the countdown band — an
            # indicator of early warning, not a judged truth.
            "countdown_weeks": cd_weeks,
            "countdown_near_rate": round(sum(r["countdown_near"] for r in pos) / cd_weeks, 3) if cd_weeks else None,
            "weeks": weeks,
            "weeks_exact": sum(r["weeks_exact"] for r in subset),
            "weeks_near": sum(r["weeks_near"] for r in subset),
            "exact_rate": round(sum(r["weeks_exact"] for r in subset) / weeks, 3) if weeks else None,
            "near_rate": round(sum(r["weeks_near"] for r in subset) / weeks, 3) if weeks else None,
        }

    unflagged = [r for r in rows if not r["flags"]]
    foresight = [{"company": r["company"], "as_of": s["as_of"], "mentions": s["foresight"]}
                 for r in rows for s in r["steps"] if s.get("foresight")]
    report = {
        # Steps whose reasoning names a date after their own: the model
        # knew, it did not read. Few is normal; many means the numbers
        # above are not a forecast.
        "foresight": foresight,
        "kind": "credit_risk",
        "companies": rows,
        "all": tally(rows),
        "unflagged": tally(unflagged),
        "flagged": [{"company": r["company"], "flags": r["flags"]} for r in rows if r["flags"]],
        # Companies with no ground truth: listed, never scored either way.
        "unverified": [{"company": r["company"], "reason": r.get("unverified_reason")}
                       for r in rows if r["type"] not in ("positive", "negative")],
        "no_ground_truth": not any(r["type"] in ("positive", "negative") for r in rows),
        "failed_steps": sum(r["failed_steps"] for r in rows),
    }
    report["headline"] = headline(report)
    return report


def headline(report: dict) -> str:
    """One line for a list row: 'detected 3/3 · false alarms 0/3 · lead 51d · ±1 100%'."""
    t = report.get("unflagged") or report.get("all") or {}
    parts = []
    if report.get("no_ground_truth"):
        n = len(report.get("unverified") or [])
        return f"no ground truth: {n} compan{'y' if n == 1 else 'ies'} without a verified outcome"
    if t.get("positives"):
        parts.append(f"detected {t['detected']}/{t['positives']}")
    if t.get("negatives"):
        parts.append(f"false alarms {t['false_alarms']}/{t['negatives']}")
    if t.get("unverified_negatives"):
        parts.append(f"{t['unverified_negatives']} negatives unscored")
    if t.get("mean_lead_days") is not None:
        parts.append(f"lead {t['mean_lead_days']}d")
    if t.get("near_rate") is not None:
        parts.append(f"±1 band {round(t['near_rate'] * 100)}%")
    if t.get("countdown_near_rate") is not None:
        parts.append(f"±1 countdown {round(t['countdown_near_rate'] * 100)}%")
    if t.get("unverified"):
        parts.append(f"{t['unverified']} unverified excluded")
    if report.get("flagged"):
        parts.append(f"{len(report['flagged'])} flagged excluded")
    if report.get("foresight"):
        parts.append(f"{len(report['foresight'])} foresight step(s)")
    return " · ".join(parts)


def metric_report(batch: dict, result_of, metric: str, label_key: str) -> dict:
    """Score every finished record with `metric` against `inputs[label_key]`."""
    if not label_key:
        raise evaluation.EvaluationError("Say which field holds the expected answer.")
    items = []
    for item in batch.get("items") or []:
        inputs = item.get("inputs") or {}
        if label_key not in inputs:
            raise evaluation.EvaluationError(
                f"Record {item.get('index', '?')} has no '{label_key}' field. "
                f"Available: {sorted(inputs)[:8]}")
        entry = {"index": item.get("index"), "status": item.get("status"),
                 "label": inputs[label_key], "score": None, "detail": None}
        if item.get("status") == "success":
            scored = evaluation.score_one(metric, result_of(item), inputs[label_key])
            entry["score"] = scored.get("score")
            extra = {k: v for k, v in scored.items() if k != "score"}
            entry["detail"] = extra or None
        items.append(entry)
    summary = evaluation.summarise(items)
    mean = summary.get("mean")
    return {
        "kind": "metric", "metric": metric, "label_key": label_key,
        "items": items, "summary": summary,
        "headline": (f"{metric} {mean:.3f} · {summary['scored']}/{summary['total']} scored"
                     if mean is not None else f"{metric} · nothing scored"),
    }
