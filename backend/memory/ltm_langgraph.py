"""LangGraph backend for the memory layer's long-term memory.

Same public interface as memory/ltm.py (open_memory / list_entries /
unquote_content); the returned object duck-types the framework
LongTermMemory surface Studio uses: add(messages) / search(query, n) /
delete(ids) / save() / load() / close().

Implementation: a LangGraph `SqliteStore` with a vector index (sqlite-vec),
one `store.sqlite` per store dir, entries under the namespace
("memory", <corpus id>). Embeddings are the same BAAI/bge-small-en-v1.5
model as the framework backend, loaded once per process and shared.

Writes are committed as they happen, so save() has nothing left to do.

NOTE: stores are NOT interchangeable with the framework backend — switching
EAX_MEMORY_BACKEND starts a fresh memory for existing stores.
"""

import hashlib
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .ltm import EMBEDDING_DIM, EMBEDDING_MODEL, shared_sentence_transformer, unquote_content  # noqa: F401

_STORE_FILE = "store.sqlite"
_NAMESPACE = "memory"
# Every entry of a store at once, for a listing: SqliteStore pages its search.
_PAGE = 1000


def _embed(texts):
    """LangGraph's embedding hook: the shared sentence-transformer."""
    model = shared_sentence_transformer(EMBEDDING_MODEL, device="cpu")
    return model.encode(list(texts), normalize_embeddings=True).tolist()


def _content_of(message) -> str:
    import json
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    if content is None:
        content = str(message)
    return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)


def _connect(path: Path):
    from langgraph.store.sqlite import SqliteStore
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    store = SqliteStore(conn, index={"dims": EMBEDDING_DIM, "embed": _embed, "fields": ["content"]})
    store.setup()
    return conn, store


class LGLongTermMemory:
    """LangGraph-backed per-agent long-term memory."""

    def __init__(self, store_dir, corpus_id: str):
        self.store_dir = Path(store_dir)
        self.corpus_id = corpus_id
        self.namespace = (_NAMESPACE, corpus_id)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self._conn, self._store = _connect(self.store_dir / _STORE_FILE)
        self._lock = threading.Lock()

    # -- persistence --------------------------------------------------------

    def save(self, *_args, **_kwargs) -> None:
        """Every write is already committed."""

    def load(self, *_args, **_kwargs) -> list:
        return [item["memory_id"] for item in self._all()]

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def _all(self) -> list[dict]:
        entries, offset = [], 0
        while True:
            page = self._store.search(self.namespace, limit=_PAGE, offset=offset)
            entries.extend(item.value for item in page)
            if len(page) < _PAGE:
                return entries
            offset += _PAGE

    # -- mutation / retrieval ------------------------------------------------

    def add(self, messages) -> list[str]:
        if not isinstance(messages, list):
            messages = [messages]
        memory_ids = []
        with self._lock:
            for message in messages:
                content = _content_of(message)
                if not content:
                    continue
                content_hash = hashlib.sha256(content.encode()).hexdigest()
                if self._store.search(self.namespace, filter={"content_hash": content_hash}, limit=1):
                    continue  # dedup, same as the framework backend
                memory_id = str(uuid.uuid4())
                self._store.put(self.namespace, memory_id, {
                    "memory_id": memory_id,
                    "content": content,
                    "content_hash": content_hash,
                    "timestamp": getattr(message, "timestamp", None) or time.strftime("%Y-%m-%d %H:%M:%S"),
                    "agent": getattr(message, "agent", None),
                    "wf_task": getattr(message, "wf_task", None),
                    "wf_goal": getattr(message, "wf_goal", None),
                    "msg_type": str(getattr(message, "msg_type", "") or ""),
                })
                memory_ids.append(memory_id)
        return memory_ids

    def delete(self, memory_ids) -> list[bool]:
        if not isinstance(memory_ids, list):
            memory_ids = [memory_ids]
        done = []
        with self._lock:
            for memory_id in memory_ids:
                existed = self._store.get(self.namespace, str(memory_id)) is not None
                if existed:
                    self._store.delete(self.namespace, str(memory_id))
                done.append(existed)
        return done

    def search(self, query, n: int | None = None, **_kwargs):
        """Vector search; returns [(evoagentx Message, memory_id)] like the
        framework backend — the second item is the entry's id, not a score."""
        from evoagentx.core.message import Message
        hits = []
        for item in self._store.search(self.namespace, query=str(query), limit=n or 5):
            entry = item.value
            hits.append((
                Message(content=entry["content"], timestamp=entry.get("timestamp"),
                        agent=entry.get("agent"), wf_task=entry.get("wf_task"),
                        wf_goal=entry.get("wf_goal")),
                entry["memory_id"],
            ))
        return hits


def open_memory(store_dir, corpus_id: str, create: bool = False):
    """Open the LangGraph-backed LTM at store_dir; None if absent and not create."""
    path = Path(store_dir)
    if not create and not (path / _STORE_FILE).is_file():
        return None
    return LGLongTermMemory(path, corpus_id)


def list_entries(store_dir) -> list[dict]:
    """All stored entries of the store at store_dir, oldest first."""
    path = Path(store_dir)
    if not (path / _STORE_FILE).is_file():
        return []
    conn, store = _connect(path / _STORE_FILE)
    try:
        entries, offset = [], 0
        for namespace in store.list_namespaces(prefix=(_NAMESPACE,)):
            offset = 0
            while True:
                page = store.search(namespace, limit=_PAGE, offset=offset)
                entries.extend(dict(item.value) for item in page)
                if len(page) < _PAGE:
                    break
                offset += _PAGE
    finally:
        conn.close()
    for entry in entries:
        entry.pop("content_hash", None)
    entries.sort(key=lambda e: e.get("timestamp") or "")
    return entries
