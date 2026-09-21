"""Persistent workflow experiments on daily, weekly, or interval schedules.

Each occurrence reuses the experiment session and workflow Memory. A durable
claim is reconciled against the run before advancing the original due cursor.
Recovery policy is user-selected: ask, replay all, latest only, or skip missed
occurrences. Schedule timing is separate from Input Watch polling.
"""

import json
import math
import os
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import threading
import traceback
from datetime import timezone, datetime, timedelta
from pathlib import Path

from backend.api import runner
from backend.api.studio_config import data_path

SCHEDULES_DIR = data_path("schedules")

MODES = ("daily", "weekly", "interval")
RECOVERY_POLICIES = ("ask", "all", "latest", "skip")
# A schedule runs LLM calls with nobody watching. A typo of 1 instead of 60
# should not be able to empty an account overnight.
MIN_INTERVAL_MINUTES = 5

_threads: dict[str, tuple[threading.Thread, threading.Event]] = {}
_lock = threading.RLock()
_state_lock = threading.RLock()


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
        "inputs": body.get("inputs") if body.get("inputs") is not None else {},
        "session": (body.get("session") or "").strip() or None,
        "recovery_policy": body.get("recovery_policy", "ask"),
        "timezone": body.get("timezone") or None,
        "time_input": (body.get("time_input") or "").strip(),
    }
    if not isinstance(schedule["inputs"], dict):
        raise ScheduleError("inputs must be a JSON object")
    if schedule["recovery_policy"] not in RECOVERY_POLICIES:
        raise ScheduleError("Unknown recovery policy")
    if schedule['timezone']:
        try:
            ZoneInfo(schedule['timezone'])
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            raise ScheduleError("Use a valid IANA timezone")
    if mode == "weekly":
        day = body.get("weekday", 0)
        if isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6:
            raise ScheduleError("weekday must be 0 (Monday) through 6 (Sunday)")
        schedule['weekday'] = day
    if mode in ("daily", "weekly"):
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
            minutes = float(body.get("interval_minutes", 60))
        except (TypeError, ValueError):
            raise ScheduleError("interval_minutes must be a number")
        if not math.isfinite(minutes) or minutes < MIN_INTERVAL_MINUTES:
            raise ScheduleError(
                f"interval_minutes must be at least {MIN_INTERVAL_MINUTES} — a "
                "schedule runs the workflow unattended, and each run costs."
            )
        schedule["interval_minutes"] = minutes
    return schedule


def next_local_time(after: datetime, hour: int, minute: int) -> datetime:
    """The next hour:minute on `after`'s wall clock, strictly after `after`.

    Callers pass `datetime.now().astimezone()`, whose tzinfo is a fixed UTC
    offset. Kept across a daylight-saving change, that offset would fire an
    hour early (twice in one day) or an hour late; so a target in the
    system's local zone is localized again for its own date."""
    wall = after.replace(tzinfo=None)
    target = wall.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= wall:
        target = datetime.combine(target.date() + timedelta(days=1), target.time())
    if after.tzinfo is None:
        return target
    local_offset = after.astimezone().utcoffset()
    if isinstance(after.tzinfo, timezone) and after.utcoffset() == local_offset:
        return target.astimezone()      # naive -> system local, DST-aware
    return target.replace(tzinfo=after.tzinfo)


def next_fire(schedule: dict, after: datetime) -> datetime:
    """When this schedule is due next, strictly after `after`."""
    if schedule.get("timezone"):
        after = after.astimezone(ZoneInfo(schedule['timezone']))
    if schedule.get("mode") in ("daily", "weekly"):
        hour, minute = (int(p) for p in (schedule.get("time") or "09:00").split(":")[:2])
        result = next_local_time(after, hour, minute)
        if schedule.get('mode') == 'weekly':
            result += timedelta(days=(int(schedule.get('weekday', 0)) - result.weekday()) % 7)
        return result
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
    target = _path(graph_id)
    temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(schedule, stream, indent=2, ensure_ascii=False, default=str)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return schedule


def set_schedule(graph: dict, body: dict) -> dict:
    """Update configuration; pausing preserves the due cursor and experiment."""
    graph_id = graph['id']
    with _state_lock:
        existing = load(graph_id) or {}
        config = validate({**existing, **body})
        cadence = ('mode', 'time', 'weekday', 'interval_minutes', 'timezone')
        changed = any(existing.get(k) != config.get(k) for k in cadence)
        if existing.get('in_flight') and changed:
            raise ScheduleError('Finish or recover the current occurrence before changing its frequency.')
        schedule = {**existing, **config}
        experiment = existing.get('experiment_id') or uuid.uuid4().hex
        schedule['experiment_id'] = experiment
        schedule['session'] = config['session'] or existing.get('session') or f'schedule:{experiment}'
        schedule.setdefault('fires', 0)
        schedule.setdefault('last_fire', None)
        schedule.setdefault('last_run_id', None)
        schedule.setdefault('last_error', None)
        if changed or not schedule.get('next_fire'):
            anchor = (_parse(existing.get('last_completed_due') or existing.get('last_fire')) if not changed else None) or _now()
            schedule['next_fire'] = next_fire(schedule, anchor).isoformat()
        if existing.get('enabled') is False and schedule['enabled'] and not changed:
            schedule['needs_resume'] = True
            schedule['last_error'] = 'Choose a recovery strategy and Resume the existing experiment.'
        stop(graph_id)
        _save(graph_id, schedule)
        if schedule['enabled'] and not schedule.get('needs_resume'):
            _start_thread(graph, schedule)
    return status(graph_id)


def _advance(schedule, due):
    schedule['last_completed_due'] = due
    schedule['next_fire'] = next_fire(schedule, _parse(due)).isoformat()
    schedule['in_flight'] = None
    schedule['needs_resume'] = False
    schedule['last_error'] = None


def _reconcile(schedule):
    occurrence = schedule.get('in_flight')
    if not occurrence:
        return False
    run = runner.get_run(occurrence['run_id'])
    if run and run.get('status') == 'success':
        _advance(schedule, occurrence['due'])
        return False
    if run and run.get('status') in ('running', 'pending', 'queued', 'stopping'):
        return True
    schedule['needs_resume'] = True
    schedule['last_error'] = (run or {}).get('error') or 'Scheduled occurrence was interrupted. Choose a recovery strategy and Resume.'
    return False


def resume(graph_id, recovery_policy):
    if recovery_policy not in ('all', 'latest', 'skip'):
        raise ScheduleError('Choose all, latest, or skip for missed occurrences.')
    with _state_lock:
        schedule = load(graph_id)
        if not schedule:
            raise ScheduleError('No schedule exists for this workflow.')
        if _reconcile(schedule):
            raise ScheduleError('An occurrence is still running; it will finish before the next one starts.')
        stop(graph_id)
        now = _now()
        anchor = _parse(schedule.get('last_completed_due') or schedule.get('last_fire')) or now
        due = _parse(schedule.get('next_fire')) or next_fire(schedule, anchor)
        if recovery_policy != 'all' and due <= now:
            # Intervals may have millions of missed ticks; advance arithmetically.
            if schedule.get('mode') == 'interval':
                step = timedelta(minutes=max(float(schedule.get('interval_minutes') or 60), MIN_INTERVAL_MINUTES))
                count = (now - due) // step + (1 if recovery_policy == 'skip' else 0)
                due += count * step
            else:
                while due <= now:
                    following = next_fire(schedule, due)
                    if recovery_policy == 'latest' and following > now:
                        break
                    due = following
        schedule.update(enabled=True, needs_resume=False, in_flight=None,
                        last_recovery_policy=recovery_policy, last_error=None,
                        next_fire=due.isoformat())
        _save(graph_id, schedule)
        _start_thread({'id': graph_id}, schedule)
    return status(graph_id)


def clear(graph_id: str) -> bool:
    """Remove a workflow's schedule entirely."""
    with _state_lock:
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
    graph_id = graph['id']
    with _lock:
        current = _threads.get(graph_id)
        if current and current[0].is_alive() and not current[1].is_set():
            return
        event = threading.Event()
        thread = threading.Thread(target=_loop, args=(graph_id, event), daemon=True)
        _threads[graph_id] = (thread, event)
        thread.start()


def _loop(graph_id, stop_event):
    try:
        while not stop_event.is_set():
            schedule = load(graph_id)
            if not schedule or not schedule.get('enabled') or schedule.get('needs_resume'):
                return
            due = _parse(schedule.get('next_fire')) or next_fire(schedule, _now())
            if not schedule.get('in_flight') and _now() < due:
                stop_event.wait(min(30, max(.1, (due - _now()).total_seconds())))
                continue
            _fire(graph_id, schedule, stop_event)
            stop_event.wait(1)
    finally:
        with _lock:
            current = _threads.get(graph_id)
            if current and current[1] is stop_event:
                _threads.pop(graph_id, None)


def _fire(graph_id, schedule, stop_event=None):
    """Durably claim one occurrence, observe completion before advancing."""
    from backend.api import graphs as graph_store
    with _state_lock:
        if stop_event and stop_event.is_set():
            return
        schedule = load(graph_id) or schedule
        if not schedule.get('enabled') or schedule.get('needs_resume'):
            return
        if schedule.get('in_flight'):
            _reconcile(schedule)
            _save(graph_id, schedule)
            return
        due = schedule.get('next_fire') or next_fire(schedule, _now()).isoformat()
        experiment = schedule.setdefault('experiment_id', uuid.uuid4().hex)
        schedule['session'] = schedule.get('session') or f'schedule:{experiment}'
        run_id = uuid.uuid5(uuid.NAMESPACE_URL, f'evostudio:{experiment}:{due}').hex[:20]
        schedule['in_flight'] = {'due': due, 'run_id': run_id}
        schedule['last_run_id'] = run_id
        schedule['last_fire'] = _now().isoformat()
        schedule['fires'] = int(schedule.get('fires') or 0) + 1
        _save(graph_id, schedule)  # claim survives a crash before dispatch
        try:
            graph = graph_store.load_graph(graph_id)
            if graph is None:
                raise ScheduleError(f"Workflow '{graph_id}' no longer exists")
            inputs = dict(schedule.get('inputs') or {})
            if schedule.get('time_input'):
                inputs[schedule['time_input']] = due
            returned = runner.start_run(graph, inputs, background=True,
                                       session=schedule['session'], run_id=run_id,
                                       session_started_at=due)
            schedule['last_run_id'] = returned
            schedule['in_flight']['run_id'] = returned
            schedule['last_error'] = None
        except Exception:
            schedule['needs_resume'] = True
            schedule['last_error'] = traceback.format_exc().strip().split('\n')[-1]
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
    from backend.api import graphs as graph_store
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
        if schedule.get('in_flight') and _reconcile(schedule):
            _start_thread({'id': graph_id}, schedule)
            started.append(graph_id)
            continue
        due = _parse(schedule.get('next_fire'))
        if due is None:
            anchor = _parse(schedule.get('last_completed_due') or schedule.get('last_fire')) or _now()
            due = next_fire(schedule, anchor)
            schedule['next_fire'] = due.isoformat()
        missed = due is not None and due <= _now()
        if schedule.get('needs_resume') or missed:
            recovery = schedule.get('recovery_policy', 'ask')
            if recovery == 'ask':
                schedule['needs_resume'] = True
                schedule['last_error'] = schedule.get('last_error') or 'Missed occurrences: choose a recovery strategy and Resume.'
                _save(graph_id, schedule)
                continue
            _save(graph_id, schedule)
            resume(graph_id, recovery)
        else:
            _save(graph_id, schedule)
            _start_thread({"id": graph_id}, schedule)
        started.append(graph_id)
    return started


def rename(old_id: str, new_id: str) -> None:
    """Follow a renamed workflow, so its schedule keeps firing."""
    with _state_lock:
        schedule = load(old_id)
        if schedule is None:
            return
        stop(old_id)
        _save(new_id, schedule)
        _path(old_id).unlink(missing_ok=True)
        if schedule.get("enabled") and not schedule.get('needs_resume'):
            _start_thread({"id": new_id}, schedule)
