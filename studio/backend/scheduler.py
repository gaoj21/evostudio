"""Running a workflow on a timer.

Distinct from watchers, which poll a *source* and run only when new data turns
up. A schedule is about the clock: run this workflow every day at nine, or
every N minutes, with these inputs, whether or not anything changed.

One schedule per workflow, kept at studio/data/schedules/<graph_id>.json rather
than in the graph — a schedule is how a workflow is operated here, not part of
what it is, and exporting one onto someone else's machine would start firing
runs they never asked for.

Schedules are restored when the process starts. "Every day" that stops at the
next restart is not a schedule, and a fire missed while the machine was off is
run once on the way back up rather than silently skipped: for a daily report,
late is almost always better than never.

Each fire starts an ordinary background run, so it shows up in run history and
on the canvas like any other. That also means it spends money unattended,
which is why the interval has a floor.
"""

import json
import threading
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from . import runner
from .studio_config import data_path

SCHEDULES_DIR = data_path("schedules")

MODES = ("daily", "interval")
# A schedule runs LLM calls with nobody watching. A typo of 1 instead of 60
# should not be able to empty an account overnight.
MIN_INTERVAL_MINUTES = 5

_threads: dict[str, tuple[threading.Thread, threading.Event]] = {}
_lock = threading.Lock()


class ScheduleError(Exception):
    """User-facing schedule validation error (HTTP 422)."""


def _path(graph_id: str) -> Path:
    return SCHEDULES_DIR / f"{graph_id}.json"


def _now() -> datetime:
    return datetime.now().astimezone()


def validate(body: dict) -> dict:
    """A schedule, normalised, or a complaint about why it is not one."""
    mode = (body.get("mode") or "daily").strip()
    if mode not in MODES:
        raise ScheduleError(f"mode must be one of {list(MODES)}, got {mode!r}")

    schedule = {
        "enabled": bool(body.get("enabled", True)),
        "mode": mode,
        "inputs": body.get("inputs") or {},
        "session": (body.get("session") or "").strip() or None,
    }
    if mode == "daily":
        at = (body.get("time") or "09:00").strip()
        try:
            hour, minute = (int(part) for part in at.split(":")[:2])
            if not (0 <= hour < 24 and 0 <= minute < 60):
                raise ValueError
        except (ValueError, TypeError):
            raise ScheduleError(f"time must be HH:MM, got {at!r}")
        schedule["time"] = f"{hour:02d}:{minute:02d}"
    else:
        try:
            minutes = float(body.get("interval_minutes") or 60)
        except (TypeError, ValueError):
            raise ScheduleError("interval_minutes must be a number")
        if minutes < MIN_INTERVAL_MINUTES:
            raise ScheduleError(
                f"interval_minutes must be at least {MIN_INTERVAL_MINUTES} — a "
                "schedule runs the workflow unattended, and each run costs."
            )
        schedule["interval_minutes"] = minutes
    return schedule


def next_fire(schedule: dict, after: datetime) -> datetime:
    """When this schedule is due next, strictly after `after`."""
    if schedule.get("mode") == "daily":
        hour, minute = (int(p) for p in (schedule.get("time") or "09:00").split(":")[:2])
        target = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= after:
            target += timedelta(days=1)
        return target
    minutes = max(float(schedule.get("interval_minutes") or 60), MIN_INTERVAL_MINUTES)
    return after + timedelta(minutes=minutes)


def load(graph_id: str) -> dict | None:
    path = _path(graph_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _save(graph_id: str, schedule: dict) -> dict:
    SCHEDULES_DIR.mkdir(parents=True, exist_ok=True)
    schedule["graph_id"] = graph_id
    try:
        _path(graph_id).write_text(
            json.dumps(schedule, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except OSError:
        pass
    return schedule


def set_schedule(graph: dict, body: dict) -> dict:
    """Create or replace a workflow's schedule, and start or stop its thread."""
    graph_id = graph.get("id")
    schedule = validate(body)
    existing = load(graph_id) or {}
    schedule.update({
        "fires": existing.get("fires", 0),
        "last_fire": existing.get("last_fire"),
        "last_run_id": existing.get("last_run_id"),
        "last_error": existing.get("last_error"),
    })
    schedule["next_fire"] = (next_fire(schedule, _now()).isoformat()
                             if schedule["enabled"] else None)
    _save(graph_id, schedule)

    stop(graph_id)
    if schedule["enabled"]:
        _start_thread(graph, schedule)
    return status(graph_id)


def clear(graph_id: str) -> bool:
    """Remove a workflow's schedule entirely."""
    stop(graph_id)
    path = _path(graph_id)
    if not path.is_file():
        return False
    path.unlink(missing_ok=True)
    return True


def stop(graph_id: str) -> bool:
    with _lock:
        found = _threads.pop(graph_id, None)
    if found is None:
        return False
    _thread, event = found
    event.set()
    return True


def status(graph_id: str) -> dict:
    schedule = load(graph_id)
    if schedule is None:
        return {"graph_id": graph_id, "scheduled": False}
    with _lock:
        running = graph_id in _threads
    return {**schedule, "scheduled": True, "running": running}


def _start_thread(graph: dict, schedule: dict) -> None:
    graph_id = graph.get("id")
    event = threading.Event()
    thread = threading.Thread(target=_loop, args=(graph_id, event), daemon=True)
    with _lock:
        _threads[graph_id] = (thread, event)
    thread.start()


def _loop(graph_id: str, stop_event: threading.Event) -> None:
    """Wait until due, run, record, repeat.

    The schedule is re-read each time round rather than captured: editing it
    should take effect at the next fire, not at the next restart.
    """
    while not stop_event.is_set():
        schedule = load(graph_id)
        if schedule is None or not schedule.get("enabled"):
            return

        due = _parse(schedule.get("next_fire")) or next_fire(schedule, _now())
        while not stop_event.is_set() and _now() < due:
            # Short waits so an edit or a stop is noticed promptly.
            stop_event.wait(min(30.0, max(1.0, (due - _now()).total_seconds())))
        if stop_event.is_set():
            return

        _fire(graph_id, schedule)


def _fire(graph_id: str, schedule: dict) -> None:
    """Start one scheduled run and write down what happened."""
    from . import graphs as graph_store
    fired_at = _now()
    schedule = load(graph_id) or schedule
    schedule["last_fire"] = fired_at.isoformat()
    schedule["fires"] = int(schedule.get("fires") or 0) + 1
    try:
        graph = graph_store.load_graph(graph_id)
        if graph is None:
            raise ScheduleError(f"Workflow '{graph_id}' no longer exists")
        schedule["last_run_id"] = runner.start_run(
            graph, schedule.get("inputs") or {},
            background=True, session=schedule.get("session"),
        )
        schedule["last_error"] = None
    except Exception:
        # A fire that cannot start must not kill the schedule: the next one may
        # well work, and the reason is worth keeping where it can be seen.
        schedule["last_run_id"] = None
        schedule["last_error"] = traceback.format_exc().strip().split("\n")[-1]
    schedule["next_fire"] = next_fire(schedule, fired_at).isoformat()
    _save(graph_id, schedule)


def _parse(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def restore() -> list[str]:
    """Start every enabled schedule again after a restart.

    A fire that came due while the process was down runs once now rather than
    being skipped: `next_fire` is already in the past, so the loop's wait ends
    immediately.
    """
    from . import graphs as graph_store
    started = []
    if not SCHEDULES_DIR.is_dir():
        return started
    for path in sorted(SCHEDULES_DIR.glob("*.json")):
        graph_id = path.stem
        schedule = load(graph_id)
        if not schedule or not schedule.get("enabled"):
            continue
        if graph_store.load_graph(graph_id) is None:
            continue
        _start_thread({"id": graph_id}, schedule)
        started.append(graph_id)
    return started


def rename(old_id: str, new_id: str) -> None:
    """Follow a renamed workflow, so its schedule keeps firing."""
    schedule = load(old_id)
    if schedule is None:
        return
    stop(old_id)
    _path(old_id).unlink(missing_ok=True)
    _save(new_id, schedule)
    if schedule.get("enabled"):
        _start_thread({"id": new_id}, schedule)
