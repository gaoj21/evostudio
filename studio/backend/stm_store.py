"""Short-term memory for EvoAgentX Studio: what happened earlier in a session.

Long-term memory is a vector store searched by similarity, and it has no sense
of order or of "this time round" — a node asking it a question gets the three
most similar things it ever saw. That is the wrong shape for the other kind of
remembering: what the workflow itself did a few minutes ago, in order, as part
of the same piece of work.

So a run may carry a `session`. Runs sharing one share a short-term log, kept
at studio/data/stm/<graph_id>.json and bucketed by session id. Nothing is
shared without a session: two records of a batch are usually independent, and
letting one contaminate the next by default would be worse than forgetting.

A session log is a log, not an index: it is read back in order, newest last,
capped by how many entries the node asked for.
"""

import threading
from pathlib import Path
from studio_config import data_path

_REPO_ROOT = Path(__file__).resolve().parents[2]

STM_DIR = data_path("stm")

# Sessions accumulate; a workflow left running for a week should not grow an
# unbounded file. Oldest entries of a session are dropped first.
MAX_PER_SESSION = 200

_lock = threading.Lock()


def _path(graph_id: str) -> Path:
    return STM_DIR / f"{graph_id}.json"


def open_store(graph_id: str):
    """The short-term memory for a workflow, loaded from disk."""
    from memory import ShortTermMemory

    return ShortTermMemory(persist_to=str(_path(graph_id)))


def append(graph_id: str, session: str, entry: dict) -> None:
    """Record one thing a node did, as part of a session."""
    if not session:
        return
    with _lock:
        store = open_store(graph_id)
        store.append(session, entry)
        kept = store.get(session)
        if len(kept) > MAX_PER_SESSION:
            store.clear(session)
            for old in kept[-MAX_PER_SESSION:]:
                store.append(session, old)


def recent(graph_id: str, session: str, n: int) -> list[dict]:
    """The last `n` entries of a session, oldest first."""
    if not session or n <= 0:
        return []
    return open_store(graph_id).get(session, n)


def sessions(graph_id: str) -> list[str]:
    """Every session this workflow has a short-term log for."""
    if not _path(graph_id).is_file():
        return []
    return open_store(graph_id).sessions()


def clear(graph_id: str, session: str | None = None) -> None:
    with _lock:
        open_store(graph_id).clear(session)
