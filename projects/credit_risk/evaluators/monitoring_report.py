"""Credit-risk monitoring report, as a canvas evaluator.

Drop this file onto an Evaluator node (timing: after the entire batch). It
is ordinary evaluator code: Studio hands it every saved run of the batch and
it decides everything else. Per trajectory (one company across its dated
steps): was a high/critical level reached before the verified event, how
many days ahead, and did a verified negative raise one. Per step: how its
level compares with the band the countdown to the event implies (reported
beside reviewed labels, never as truth), and whether the reasoning names a
date after the step's own (the model used what it knows, not the material).

Truth comes from, in order:
  * label_records (the Evaluator's "Separate labels"): rows with
    {case_id, type: positive|negative|unverified, event_date}; see labels.py;
  * the record's own sample_json, when the dataset carries it (contemporary).
A trajectory with neither is listed as unverified and never scored.
"""
import json
import re
from datetime import date
from statistics import fmean

BANDS = ("low", "medium", "high", "critical")
ALERT_LEVELS = ("high", "critical")
MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"])}


def _json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _decision(value):
    parsed = _json(value)
    if isinstance(parsed, dict):
        return parsed
    if not isinstance(value, str):
        return None
    # A stray quote from the model: read the fields off the text.
    out = {}
    for key in ("action", "risk_level", "rationale"):
        m = re.search(rf'"{key}"\s*:\s*"([^"]*)"', value)
        if m:
            out[key] = m.group(1)
    m = re.search(r'"score"\s*:\s*(\d+(?:\.\d+)?)', value)
    if m:
        out["score"] = float(m.group(1))
    return out or None


def _band(level):
    try:
        return BANDS.index(str(level).lower())
    except ValueError:
        return None


def future_mentions(text, as_of):
    if not as_of or not text:
        return []
    found = [m.group(0) for m in re.finditer(r"\b(20\d\d)-(\d\d)-(\d\d)\b", text) if m.group(0) > as_of]
    for m in re.finditer(r"\b(January|February|March|April|May|June|July|August|September|"
                         r"October|November|December)\s+(20\d\d)\b", text):
        if f"{m.group(2)}-{MONTHS[m.group(1).lower()]:02d}-01" > as_of:
            found.append(m.group(0))
    return sorted(set(found))


def countdown_band(event_date, as_of):
    try:
        days = (date.fromisoformat(event_date) - date.fromisoformat(as_of)).days
    except (TypeError, ValueError):
        return None
    return "critical" if days <= 30 else "high" if days <= 90 else "medium"


class MonitoringReport:
    def __init__(self, decision_field, trajectory_field, date_field, label_key, reasoning_nodes, label_records):
        self.decision_field = decision_field
        self.trajectory_field = trajectory_field
        self.date_field = date_field
        self.reasoning_nodes = [n.strip() for n in reasoning_nodes.split(",") if n.strip()]
        self.labels = {}
        for row in label_records or []:
            key = row.get(label_key)
            if key is not None:
                self.labels[str(key)] = row

    def _truth(self, key, sample):
        if key in self.labels:
            row = self.labels[key]
            return row.get("type") or "unverified", row.get("event_date"), row.get("reason"), []
        if sample.get("type") in ("positive", "negative"):
            label = sample.get("label") or {}
            flags = list((sample.get("news_quality") or {}).get("flags") or [])
            kind = sample["type"]
            if kind == "negative" and label.get("negative_review_status") not in (None, "verified"):
                kind = "unverified"
            return kind, label.get("event_date"), None, flags
        return "unverified", None, "no verified outcome for this trajectory", []

    def evaluate(self, records):
        companies = {}
        for record in records:
            inputs = record.get("inputs") or {}
            sample = _json(inputs.get("sample_json")) or {}
            key = str(sample.get("sample_id") or inputs.get(self.trajectory_field) or record.get("id"))
            if key not in companies:
                kind, event_date, reason, flags = self._truth(key, sample)
                name = inputs.get("company") or (sample.get("company") or {}).get("name") or key
                companies[key] = {"trajectory": key, "company": name, "type": kind, "event_date": event_date,
                                  "unverified_reason": reason, "flags": flags, "steps": []}
            company = companies[key]
            as_of = str(inputs.get(self.date_field) or "")[:10]
            ok = record.get("status") == "success"
            result = record.get("result") or {}
            decision = _decision(result.get(self.decision_field)) if ok else None
            level = (decision or {}).get("risk_level")
            countdown = countdown_band(company["event_date"], as_of) if company["type"] == "positive" else None
            got, want = _band(level), _band(countdown)
            outputs = record.get("node_outputs") or {}
            text = " ".join(json.dumps(outputs.get(n) or {}, ensure_ascii=False) for n in self.reasoning_nodes)
            company["steps"].append({
                "as_of": as_of, "status": record.get("status"),
                "action": (decision or {}).get("action"), "risk_level": level,
                "score": (decision or {}).get("score"), "rationale": (decision or {}).get("rationale"),
                "countdown": countdown,
                "countdown_match": None if got is None or want is None else
                                   "exact" if got == want else "near" if abs(got - want) == 1 else "off",
                "foresight": future_mentions(text, as_of) if ok else [],
            })

        rows = []
        for c in companies.values():
            steps = sorted(c["steps"], key=lambda s: s["as_of"])
            alerts = [s for s in steps if s["risk_level"] in ALERT_LEVELS]
            first = alerts[0]["as_of"] if alerts else None
            lead = None
            if first and c["event_date"]:
                lead = (date.fromisoformat(c["event_date"]) - date.fromisoformat(first)).days
            judged = [s for s in steps if s["countdown_match"] is not None]
            rows.append({**c, "steps": steps,
                         "detected": bool(alerts) if c["type"] == "positive" else None,
                         "false_alarm": bool(alerts) if c["type"] == "negative" else None,
                         "first_alert": first, "lead_days": lead,
                         "countdown_steps": len(judged),
                         "countdown_near": sum(1 for s in judged if s["countdown_match"] in ("exact", "near")),
                         "failed_steps": sum(1 for s in steps if s["status"] != "success"),
                         # Per-trajectory score: caught (1) / missed (0), clean (1) / false alarm (0).
                         "score": (1.0 if alerts else 0.0) if c["type"] == "positive"
                                  else (0.0 if alerts else 1.0) if c["type"] == "negative" else None})
        order = {"positive": 0, "negative": 1}
        rows.sort(key=lambda r: (order.get(r["type"], 2), str(r["company"])))

        def tally(subset):
            pos = [r for r in subset if r["type"] == "positive"]
            neg = [r for r in subset if r["type"] == "negative"]
            leads = [r["lead_days"] for r in pos if r["lead_days"] is not None]
            cd = sum(r["countdown_steps"] for r in pos)
            return {
                "positives": len(pos), "detected": sum(1 for r in pos if r["detected"]),
                "negatives": len(neg), "false_alarms": sum(1 for r in neg if r["false_alarm"]),
                "detection_rate": sum(1 for r in pos if r["detected"]) / len(pos) if pos else None,
                "false_alarm_rate": sum(1 for r in neg if r["false_alarm"]) / len(neg) if neg else None,
                "mean_lead_days": round(fmean(leads)) if leads else None,
                "countdown_near_rate": round(sum(r["countdown_near"] for r in pos) / cd, 3) if cd else None,
                "unverified": sum(1 for r in subset if r["type"] not in ("positive", "negative")),
            }

        unflagged = tally([r for r in rows if not r["flags"]])
        everything = tally(rows)
        foresight = [{"company": r["company"], "as_of": s["as_of"], "mentions": s["foresight"]}
                     for r in rows for s in r["steps"] if s["foresight"]]
        scored = sum(1 for r in rows if r["score"] is not None)
        return {
            "metrics": {
                "detection_rate": unflagged["detection_rate"],
                "false_alarm_rate": unflagged["false_alarm_rate"],
                "mean_lead_days": unflagged["mean_lead_days"],
                "countdown_near_rate": unflagged["countdown_near_rate"],
                "foresight_steps": len(foresight),
                "failed_steps": sum(r["failed_steps"] for r in rows),
            },
            "records": rows,
            "coverage": {"unit": "trajectories", "total": len(rows), "scored": scored, "unscored": len(rows) - scored},
            "details": {"without_flagged": unflagged, "all": everything,
                        "flagged": [{"company": r["company"], "flags": r["flags"]} for r in rows if r["flags"]],
                        "unverified": [{"company": r["company"], "reason": r["unverified_reason"]}
                                       for r in rows if r["type"] not in ("positive", "negative")],
                        "foresight": foresight},
        }


def build_evaluator(decision_field: str = "decision", trajectory_field: str = "sample_id",
                    date_field: str = "as_of", label_key: str = "case_id",
                    reasoning_nodes: str = "detect,investigate,reflect,decide",
                    label_records=None):
    return MonitoringReport(decision_field, trajectory_field, date_field, label_key, reasoning_nodes, label_records)
