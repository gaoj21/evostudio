"""Structured memory with independent record identity and optional business time."""

import json
import hashlib
import uuid
import sqlite3
import time
import threading
from pathlib import Path
from .identity import storage_name, display_names
from backend.api.studio_config import data_path

TABLES_DIR = data_path("tables")

# SQLite serialises writers itself, but a batch has several threads opening
# the same file; a short busy timeout turns contention into a wait instead of
# an error.
_BUSY_TIMEOUT_MS = 10_000
# Schema and journal mode are set up once per file, not on every connection.
# Doing it per connection made twelve writers take twelve exclusive locks
# before any of them inserted, and `PRAGMA journal_mode` does not reliably
# honour busy_timeout — so under load a writer raised and its row was simply
# never written.
_ready: set = set()
_ready_lock = threading.Lock()
# How many times a writer waits out a locked database before giving up. The
# point of the table is that a row is either there or it is not; a write that
# quietly failed would put us back where the vector store was.
_WRITE_ATTEMPTS = 5


def path(graph_id: str, node: str) -> Path:
    return TABLES_DIR / graph_id / f"{_safe(storage_name(graph_id, node))}.db"


def _safe(name: str) -> str:
    keep = [c if (c.isalnum() or c in "-_") else "-" for c in str(name)]
    return "".join(keep).strip("-") or "node"


def _connect(graph_id: str, node: str, create: bool):
    target = path(graph_id, node)
    if not create and not target.is_file():
        return None
    # Memoised per path, but the memo is not the truth: a reset (or a hand
    # that emptied the folder) removes the file underneath a live server,
    # and a process that still "remembered" it failed every later write
    # with "unable to open database file" — quietly, from inside a run
    # that then reported success. The file on disk decides.
    fresh = str(target) not in _ready or not target.is_file()
    if fresh:
        target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.create_function("eax_at", 1, _instant, deterministic=True)
    conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    if not fresh:
        return conn
    with _ready_lock:
        # Readers do not block the writer, which is what a batch needs:
        # several records of the same node write while others read.
        conn.execute("PRAGMA journal_mode = WAL")
        columns = {r[1] for r in conn.execute("PRAGMA table_info(rows_)")}
        if columns and "record_id" not in columns:
            conn.execute("BEGIN IMMEDIATE")
            # Re-check under the SQLite lock in case another process migrated it.
            columns = {r[1] for r in conn.execute("PRAGMA table_info(rows_)")}
            if "record_id" not in columns:
                conn.execute("ALTER TABLE rows_ RENAME TO legacy_rows")
                conn.execute("DROP INDEX IF EXISTS rows_subject_at")
                _create_schema(conn)
                for row in conn.execute("SELECT * FROM legacy_rows").fetchall():
                    conn.execute("INSERT INTO rows_ VALUES (?, ?, ?, ?, ?)",
                                 (_legacy_id(row['subject'], row['at']), row['subject'], row['at'], row['recorded_at'], row['payload']))
                conn.execute("DROP TABLE legacy_rows")
        _create_schema(conn)
        conn.commit()
        _ready.add(str(target))
    return conn


def _instant(value):
    """A stored time as comparable UTC text, to the microsecond.

    SQLite's julianday() keeps milliseconds, so 23:59:59.999999 rounded into
    the next day and a row from the last instant before a cutoff fell after
    it. None (like julianday's NULL) for a value that is not a time.
    """
    from datetime import datetime, timezone

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _create_schema(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS rows_ (record_id TEXT PRIMARY KEY, subject TEXT NOT NULL, at TEXT NOT NULL, recorded_at TEXT NOT NULL, payload TEXT NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS rows_subject_at ON rows_ (subject, at)")


def _legacy_id(subject, at):
    return 'legacy:' + hashlib.sha256(json.dumps([str(subject), str(at or '')]).encode()).hexdigest()


def upsert(graph_id: str, node: str, subject: str, at: str,
           payload: dict, recorded_at: str, *, record_id: str | None = None) -> None:
    """Write one dated state, replacing any row already held for that date.

    Re-running a date corrects it rather than stacking a second copy, which is
    what makes a batch safe to resume. The primary key does the work that a
    read-modify-write under a lock used to do badly.
    """
    last = None
    for attempt in range(_WRITE_ATTEMPTS):
        conn = None
        try:
            # Inside the retry: opening is where a vanished folder fails.
            conn = _connect(graph_id, node, create=True)
            conn.execute(
                "INSERT INTO rows_ (record_id, subject, at, recorded_at, payload) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(record_id) DO UPDATE SET "
                "subject = excluded.subject, at = excluded.at, recorded_at = excluded.recorded_at, payload = excluded.payload",
                (record_id or _legacy_id(subject, at), str(subject), str(at or ""), str(recorded_at),
                 json.dumps(payload, ensure_ascii=False, default=str)),
            )
            conn.commit()
            return
        except sqlite3.OperationalError as e:
            # "database is locked" under a batch's own contention. Backing off
            # and retrying is the difference between a row and a silent hole.
            # The path is also forgotten, so a file that was removed underneath
            # us has its schema rebuilt rather than failing as "no such table"
            # for the rest of the process.
            last = e
            _ready.discard(str(path(graph_id, node)))
            time.sleep(0.05 * (attempt + 1))
        finally:
            if conn is not None:
                conn.close()
    raise last


def forget(graph_id: str | None = None) -> None:
    """Drop the memo of opened files: after a reset, everything is fresh."""
    with _ready_lock:
        if graph_id is None:
            _ready.clear()
        else:
            prefix = str(TABLES_DIR / graph_id) + "/"
            for key in [k for k in _ready if k.startswith(prefix)]:
                _ready.discard(key)


def before(graph_id: str, node: str, subject: str, cutoff: str,
           limit: int = 10) -> list[dict]:
    """This subject's states from strictly before `cutoff`, oldest first.

    A point-in-time read, which is the whole reason for the table: correct by
    the WHERE clause rather than by a filter someone has to remember to apply.
    An undated row cannot be placed in time and so cannot qualify.
    """
    if not cutoff:
        return recent(graph_id, node, subject, limit)
    return _select(
        graph_id, node,
        "SELECT * FROM rows_ WHERE subject = ? AND at != '' AND eax_at(at) < eax_at(?) "
        "ORDER BY eax_at(at) DESC, recorded_at DESC, record_id DESC LIMIT ?",
        (str(subject), str(cutoff), int(limit)),
    )


def recent(graph_id: str, node: str, subject: str, limit: int = 10) -> list[dict]:
    """This subject's most recent states, oldest first, with no cutoff."""
    return _select(
        graph_id, node,
        "SELECT * FROM rows_ WHERE subject = ? ORDER BY eax_at(at) DESC, recorded_at DESC, record_id DESC LIMIT ?",
        (str(subject), int(limit)),
    )


def _select(graph_id: str, node: str, sql: str, args: tuple) -> list[dict]:
    conn = _connect(graph_id, node, create=False)
    if conn is None:
        return []
    try:
        # Taken newest-first so LIMIT keeps the *latest* states, then handed
        # back oldest-first because that is the order they are read in.
        found = [_row(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()
    return list(reversed(found))


def _row(record) -> dict:
    try:
        payload = json.loads(record["payload"])
    except (TypeError, ValueError):
        payload = {}
    return {"record_id": record["record_id"], "subject": record["subject"], "at": record["at"],
            "recorded_at": record["recorded_at"], "payload": payload}


def rows(graph_id: str, node: str, subject: str | None = None) -> list[dict]:
    """Every row, or every row for one subject, oldest first."""
    if subject is None:
        return list(reversed(_select(
            graph_id, node,
            "SELECT * FROM rows_ ORDER BY subject, at DESC", ())))
    return _select(graph_id, node,
                   "SELECT * FROM rows_ WHERE subject = ? ORDER BY at DESC",
                   (str(subject),))


def subjects(graph_id: str, node: str) -> list[str]:
    conn = _connect(graph_id, node, create=False)
    if conn is None:
        return []
    try:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT subject FROM rows_ ORDER BY subject").fetchall()]
    finally:
        conn.close()


def nodes(graph_id: str) -> list[str]:
    base = TABLES_DIR / graph_id
    if not base.is_dir():
        return []
    return display_names(graph_id, [p.stem for p in base.glob("*.db")], _safe)


def count(graph_id: str, node: str) -> int:
    """How many rows this node holds — the cheap answer to "did it write?"."""
    conn = _connect(graph_id, node, create=False)
    if conn is None:
        return 0
    try:
        return int(conn.execute("SELECT COUNT(*) FROM rows_").fetchone()[0])
    finally:
        conn.close()


def clear(graph_id: str, node: str) -> int:
    conn = _connect(graph_id, node, create=False)
    if conn is None:
        return 0
    try:
        removed = conn.execute("DELETE FROM rows_").rowcount
        conn.commit()
        return removed
    finally:
        conn.close()


def latest(graph_id, node, limit=10, cutoff=''):
    if cutoff:
        return _select(graph_id, node,
            "SELECT * FROM rows_ WHERE at != '' AND eax_at(at) < eax_at(?) ORDER BY eax_at(at) DESC, recorded_at DESC, record_id DESC LIMIT ?",
            (cutoff, int(limit)))
    return _select(graph_id, node,
        "SELECT * FROM rows_ ORDER BY recorded_at DESC, record_id DESC LIMIT ?", (int(limit),))


def write(graph_id, node, subject, at, payload, recorded_at, *, mode='append', execution_id=None, key=None):
    if mode == 'append':
        identity = 'event:' + (str(execution_id) if execution_id else uuid.uuid4().hex)
    elif key is not None:
        identity = 'key:' + hashlib.sha256(str(key).encode()).hexdigest()
    else:
        identity = _legacy_id(subject, at)
    upsert(graph_id, node, subject, at, payload, recorded_at, record_id=identity)
