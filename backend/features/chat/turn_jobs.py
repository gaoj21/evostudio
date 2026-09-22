"""Assistant turns that run on the server, independently of the HTTP request.

A chat turn (model calls, reads, canvas edits, computations) used to live as
long as the request that started it: the answer only ever reached the client
through that one response. Navigating to another panel or workflow unmounted
the chat and threw the answer away even though the server finished the work.

A background turn is keyed by its request id (the same id Stop already uses),
runs on its own thread and keeps its outcome here, so any client can poll it,
re-attach after a remount and read the result once it settles. Only an
explicit Stop — never a closed connection — cancels it.
"""
import threading
import time

from fastapi import HTTPException

from backend.api import chat_control

TTL = 3600
# What Stop reaches and what it does not, for the chat to show as it is.
STOP_NOTE = ('Stopped. A model request already sent may still be finishing on the '
             'server; nothing it answers is used.')
_lock = threading.Lock()
_turns: dict[tuple[str, str], dict] = {}


def _reap() -> None:
    cutoff = time.time() - TTL
    with _lock:
        for key in [k for k, v in _turns.items() if v['status'] != 'running' and v['updated_at'] < cutoff]:
            del _turns[key]


def _was_stopped(key) -> bool:
    with _lock:
        return bool((_turns.get(key) or {}).get('_stopped_at'))


def _set(key, **fields) -> None:
    with _lock:
        turn = _turns.get(key)
        if turn is not None:
            turn.update(fields, updated_at=time.time())


def public(turn: dict) -> dict:
    view = {k: v for k, v in turn.items() if not k.startswith('_')}
    view['elapsed'] = int((turn.get('finished_at') or time.time()) - turn['started_at'])
    if turn.get('_stopped_at') and turn['status'] == 'running':
        # Settled the moment Stop is taken. The thread may still be inside a
        # request the provider already accepted, which nothing can recall, so
        # waiting for it would leave the chat on "Stopping…" indefinitely.
        view.update(status='cancelled', stage='Stopped', stop_note=STOP_NOTE,
                    finished_at=turn['_stopped_at'],
                    result={'stopped': True, 'reply': 'Stopped.', 'operations': [], 'activity': []})
        view['elapsed'] = int(turn['_stopped_at'] - turn['started_at'])
    return view


def stop(graph_id: str, turn_id: str) -> dict | None:
    """Mark a turn stopped; returns its settled view, or None if unknown.

    The work keeps unwinding on its own thread — the cancellation it checks is
    the caller's control — but from here on the turn is cancelled: its result
    is discarded, and `running()` no longer offers it to a re-attaching chat.
    """
    with _lock:
        turn = _turns.get((graph_id, turn_id))
        if turn is None:
            return None
        if turn['status'] == 'running' and not turn.get('_stopped_at'):
            turn.update(_stopped_at=time.time(), updated_at=time.time())
        return public(dict(turn))


def start(graph_id: str, turn_id: str, context: str, work, control) -> dict:
    """Run `work()` on a thread under `control`; returns the turn's public view."""
    _reap()
    now = time.time()
    key = (graph_id, turn_id)
    turn = {'turn_id': turn_id, 'graph_id': graph_id, 'context': context, 'status': 'running',
            'stage': 'Working…', 'result': None, 'error': None, 'status_code': None,
            'started_at': now, 'updated_at': now, 'finished_at': None}
    with _lock:
        if key in _turns:
            raise HTTPException(409, 'Assistant request ID has already been used')
        _turns[key] = turn

    def run():
        token = chat_control.current.set(control)
        try:
            chat_control.check()
            result = work()
            if _was_stopped(key):
                # The answer arrived after Stop. It is not shown, and it does
                # not overwrite the settled view the chat already read.
                _set(key, status='cancelled', stage='Stopped', finished_at=time.time())
            elif isinstance(result, dict) and result.get('stopped'):
                _set(key, status='cancelled', stage='Stopped', result=result, finished_at=time.time())
            else:
                _set(key, status='done', stage='Done', result=result, finished_at=time.time())
        except chat_control.Cancelled:
            _set(key, status='cancelled', stage='Stopped',
                 result={'stopped': True, 'reply': 'Stopped.', 'operations': [], 'activity': []},
                 finished_at=time.time())
        except HTTPException as exc:
            if _was_stopped(key):
                _set(key, status='cancelled', stage='Stopped', finished_at=time.time())
            else:
                _set(key, status='failed', stage='', error=exc.detail, status_code=exc.status_code, finished_at=time.time())
        except Exception as exc:  # noqa: BLE001 - reported to the client that re-attaches
            if _was_stopped(key):
                _set(key, status='cancelled', stage='Stopped', finished_at=time.time())
            else:
                _set(key, status='failed', stage='', error=f'{type(exc).__name__}: {exc}', status_code=500,
                     finished_at=time.time())
        finally:
            if not control.background:
                control.finished = True
            control.updated = time.monotonic()
            chat_control.current.reset(token)

    threading.Thread(target=run, name=f'assistant-turn-{turn_id}', daemon=True).start()
    return public(turn)


def get(graph_id: str, turn_id: str) -> dict:
    _reap()
    with _lock:
        turn = _turns.get((graph_id, turn_id))
        if turn is None:
            raise HTTPException(404, 'Unknown or expired assistant turn')
        return public(dict(turn))


def running(graph_id: str, context: str | None = None) -> list[dict]:
    """Turns still in flight for this workflow, newest first."""
    _reap()
    with _lock:
        rows = [dict(t) for (g, _), t in _turns.items()
                if g == graph_id and t['status'] == 'running' and not t.get('_stopped_at')
                and (context is None or t['context'] == context)]
    return [public(t) for t in sorted(rows, key=lambda t: t['started_at'], reverse=True)]
