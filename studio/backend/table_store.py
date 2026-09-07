"""Table memory: one row per subject per date.

Modelled on Coze's 数据库 rather than its 知识库. A node that tracks a subject
— an obligor, a ticker, a customer — is not doing fuzzy lookup. It is keeping
a record, and a record wants a table: typed key, exact read, a row that is
either there or it is not.

That last property is the point. Memory built on a vector corpus fails
silently: a store that never attached, never wrote, or matched nothing
produces a workflow that still runs and still answers plausibly. A missing
row, by contrast, is visible — you can count them.

Bi-temporal, in the sense Zep's Graphiti uses the word. Every row carries two
times and they are never interchangeable:

    at           when the state was true in the world   (valid time)
    recorded_at  when this system learned it            (transaction time)

Only `at` may be compared against the date a run is processing. Comparing
against `recorded_at` mixes two clocks, and a batch that writes its records
out of order then reads its own future.
"""

import json
import sqlite3
import time
import threading
from pathlib import Path
from .studio_config import data_path

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
    return TABLES_DIR / graph_id / f"{_safe(node)}.db"


def _safe(name: str) -> str:
    keep = [c if (c.isalnum() or c in "-_") else "-" for c in str(name)]
    return "".join(keep).strip("-") or "node"


def _connect(graph_id: str, node: str, create: bool):
    target = path(graph_id, node)
    if not create and not target.is_file():
        return None
    fresh = str(target) not in _ready
    if fresh:
        target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    if not fresh:
        return conn
    with _ready_lock:
        # Readers do not block the writer, which is what a batch needs:
        # several records of the same node write while others read.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rows_ (
                subject     TEXT NOT NULL,
                at          TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                payload     TEXT NOT NULL,
                PRIMARY KEY (subject, at)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS rows_subject_at ON rows_ (subject, at)")
        conn.commit()
        _ready.add(str(target))
    return conn


def upsert(graph_id: str, node: str, subject: str, at: str,
           payload: dict, recorded_at: str) -> None:
    """Write one dated state, replacing any row already held for that date.

    Re-running a date corrects it rather than stacking a second copy, which is
    what makes a batch safe to resume. The primary key does the work that a
    read-modify-write under a lock used to do badly.
    """
    last = None
    for attempt in range(_WRITE_ATTEMPTS):
        conn = _connect(graph_id, node, create=True)
        try:
            conn.execute(
                "INSERT INTO rows_ (subject, at, recorded_at, payload) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(subject, at) DO UPDATE SET "
                "recorded_at = excluded.recorded_at, payload = excluded.payload",
                (str(subject), str(at or ""), str(recorded_at),
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
            conn.close()
    raise last


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
        "SELECT * FROM rows_ WHERE subject = ? AND at != '' AND at < ? "
        "ORDER BY at DESC LIMIT ?",
        (str(subject), str(cutoff), int(limit)),
    )


def recent(graph_id: str, node: str, subject: str, limit: int = 10) -> list[dict]:
    """This subject's most recent states, oldest first, with no cutoff."""
    return _select(
        graph_id, node,
        "SELECT * FROM rows_ WHERE subject = ? ORDER BY at DESC LIMIT ?",
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
    return {"subject": record["subject"], "at": record["at"],
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
    return sorted(p.stem for p in base.glob("*.db"))


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
