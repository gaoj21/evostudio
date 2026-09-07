"""Watchers: scheduled / live input-source polling for EvoAgentX Studio.

A canvas source node may carry a schedule in its config:
  {"mode": "ondemand"|"daily"|"interval", "time": "09:00", "interval_minutes": N}
`ondemand` (default) = manual runs only. POST /api/graphs/{id}/watch starts
one daemon watcher thread per scheduled source node; each fire polls the
source for NEW data and starts a normal background run per new record.
Dedup state (seen ids + fire count) persists to
studio/data/watch/<graph_id>_<node>.json so restarts don't re-fire old data.

Interval floor: 5 minutes (GDELT fair use); EAX_WATCH_DEBUG=1 allows 0.2 min
for testing.
"""

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import runner
from . import source_apis
from . import sources
from .studio_config import data_path

WATCH_DIR = data_path("watch")

MIN_INTERVAL_MINUTES = 5
DEBUG_MIN_INTERVAL_MINUTES = 0.2

_watchers: dict[tuple[str, str], dict] = {}
_lock = threading.Lock()


def _utcnow() -> str:
    return datetime.now().astimezone().isoformat()


def _min_interval() -> float:
    return DEBUG_MIN_INTERVAL_MINUTES if os.getenv("EAX_WATCH_DEBUG") == "1" else MIN_INTERVAL_MINUTES


def _seen_path(graph_id: str, node_name: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in f"{graph_id}_{node_name}")
    return WATCH_DIR / f"{safe}.json"


def _load_seen(graph_id: str, node_name: str) -> dict:
    path = _seen_path(graph_id, node_name)
    if path.is_file():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"seen": [], "fire_count": 0}


def _save_seen(graph_id: str, node_name: str, data: dict) -> None:
    WATCH_DIR.mkdir(parents=True, exist_ok=True)
    with open(_seen_path(graph_id, node_name), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Per-type polling: return a list of NEW records (one run per record)
# ---------------------------------------------------------------------------

def _poll_credit_risk(node: dict, seen: dict) -> list[dict]:
    config = dict(node.get("source") or {})
    config["seed"] = int(config.get("seed") or 42) + seen.get("fire_count", 0)
    records = sources.records_from_source_node({**node, "source": config})
    fresh = [r for r in records if r.get("sample_id") not in seen["seen"]]
    for r in fresh:
        seen["seen"].append(r.get("sample_id"))
    return fresh


def _poll_gdelt(node: dict, seen: dict, last_poll: str | None) -> list[dict]:
    config = dict(node.get("source") or {})
    start = None
    if last_poll:
        start = datetime.fromisoformat(last_poll).date()
    record = source_apis.fetch_gdelt_news(config, start=start)
    lines = [l for l in record["news_batch"].splitlines() if l.strip()]
    fresh = [l for l in lines if l.rsplit("— ", 1)[-1] not in seen["seen"]]
    for line in fresh:
        seen["seen"].append(line.rsplit("— ", 1)[-1])  # url
    if not fresh:
        return []
    return [{
        "company": record["company"],
        "news_batch": "\n".join(fresh),
        "window_start": record["window_start"],
        "window_end": record["window_end"],
        "n_articles": len(fresh),
    }]


def _poll_edgar(node: dict, seen: dict) -> list[dict]:
    config = node.get("source") or {}
    cik = str(config.get("cik") or "").strip().lstrip("0")
    _, data = source_apis._get_json(
        f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json",
        headers=source_apis.UA,
    )
    recent = (data or {}).get("filings", {}).get("recent", {})
    blocks = []
    dates = []
    for form, fdate, adsh in zip(recent.get("form", []),
                                 recent.get("filingDate", []),
                                 recent.get("accessionNumber", [])):
        if form not in source_apis.EDGAR_FORMS or adsh in seen["seen"]:
            continue
        if len(blocks) >= int(config.get("count") or 2):
            break
        time.sleep(source_apis.EDGAR_PAUSE)
        text = source_apis._html_to_text(source_apis._fetch_submission_body(cik, adsh))
        items = sorted(set(m.lower() for m in source_apis._ITEM_HEADING_RE.findall(text)))
        blocks.append(f"[{fdate}] Form {form} (items: {', '.join(items)})\n"
                      f"{text[:source_apis.EDGAR_TEXT_CAP]}")
        dates.append(fdate)
        seen["seen"].append(adsh)
    if not blocks:
        return []
    return [{
        "cik": cik,
        "company": (data or {}).get("name", ""),
        "filing_batch": "\n\n".join(blocks),
        "window_end": max(dates),
    }]


def _poll_http_api(node: dict, seen: dict) -> list[dict]:
    record = source_apis.fetch_http_api(node.get("source") or {})
    digest = hashlib.sha256(record["api_response"].encode()).hexdigest()
    if digest in seen["seen"]:
        return []
    seen["seen"].append(digest)
    return [record]


def poll_source(node: dict, seen: dict, last_poll: str | None) -> list[dict]:
    type_ = (node.get("source") or {}).get("type")
    if type_ == "credit_risk":
        return _poll_credit_risk(node, seen)
    if type_ == "gdelt_news":
        return _poll_gdelt(node, seen, last_poll)
    if type_ == "sec_edgar_8k":
        return _poll_edgar(node, seen)
    if type_ == "http_api":
        return _poll_http_api(node, seen)
    raise sources.SourceError(f"Cannot watch source type: {type_!r}")


# ---------------------------------------------------------------------------
# Watcher threads
# ---------------------------------------------------------------------------

def _next_fire(schedule: dict, from_dt: datetime) -> datetime:
    mode = schedule.get("mode")
    if mode == "daily":
        hh, mm = (schedule.get("time") or "09:00").split(":")[:2]
        target = from_dt.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if target <= from_dt:
            target += timedelta(days=1)
        return target
    # interval
    minutes = max(float(schedule.get("interval_minutes") or 60), _min_interval())
    return from_dt + timedelta(minutes=minutes)


def _watch_loop(graph: dict, node: dict, stop: threading.Event, state: dict) -> None:
    graph_id = graph.get("id")
    node_name = node.get("name")
    seen = _load_seen(graph_id, node_name)
    while not stop.is_set():
        schedule = (node.get("source") or {}).get("schedule") or {}
        nxt = _next_fire(schedule, datetime.now().astimezone())
        state["next_fire"] = nxt.isoformat()
        while not stop.is_set() and datetime.now().astimezone() < nxt:
            stop.wait(1.0)
        if stop.is_set():
            break
        state["last_poll"] = _utcnow()
        try:
            records = poll_source(node, seen, state.get("last_fire"))
            seen["fire_count"] = seen.get("fire_count", 0) + 1
            _save_seen(graph_id, node_name, seen)
            for record in records:
                run_id = runner.start_run(graph, record, background=True)
                state["fired_runs"] = state.get("fired_runs", 0) + 1
                state["last_run_id"] = run_id
            if records:
                state["last_fire"] = state["last_poll"]
            state["last_error"] = None
        except Exception as e:
            state["last_error"] = str(e)[:500]
    state["next_fire"] = None


def start_graph_watch(graph: dict) -> list[dict]:
    """Start watchers for every scheduled (non-ondemand) source node.

    Only wired source nodes (outgoing edge) count — unwired nodes are inert
    drafts, same as at run time.
    """
    wired = {e.get("source") for e in (graph.get("edges") or [])}
    started = []
    for node in sources.find_source_nodes(graph):
        if node.get("name") not in wired:
            continue
        schedule = (node.get("source") or {}).get("schedule") or {}
        if schedule.get("mode") in (None, "ondemand"):
            continue
        key = (graph.get("id"), node.get("name"))
        with _lock:
            if key in _watchers:
                continue
            stop = threading.Event()
            state = {
                "graph_id": graph.get("id"),
                "node": node.get("name"),
                "source_type": (node.get("source") or {}).get("type"),
                "schedule": schedule,
                "last_poll": None,
                "last_fire": None,
                "next_fire": None,
                "fired_runs": 0,
                "last_run_id": None,
                "last_error": None,
            }
            thread = threading.Thread(
                target=_watch_loop, args=(graph, node, stop, state), daemon=True
            )
            _watchers[key] = {"thread": thread, "stop": stop, "state": state}
            thread.start()
            started.append(state)
    return started


def stop_graph_watch(graph_id: str) -> int:
    """Stop all watchers of a graph; returns how many were stopped."""
    with _lock:
        entries = [(k, w) for k, w in _watchers.items() if k[0] == graph_id]
        for key, watcher in entries:
            watcher["stop"].set()
            del _watchers[key]
    return len(entries)


def graph_watch_status(graph_id: str) -> dict:
    with _lock:
        watchers = [w["state"] for k, w in _watchers.items() if k[0] == graph_id]
    return {"watching": bool(watchers), "watchers": watchers}
