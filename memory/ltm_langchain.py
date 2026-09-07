"""LangChain backend for the memory layer's long-term memory.

Same public interface as memory/ltm.py (open_memory / list_entries /
unquote_content); the returned object duck-types the framework
LongTermMemory surface Studio uses: add(messages) / search(query, n) /
save() / load().

Implementation: langchain_community FAISS vectorstore +
langchain_huggingface HuggingFaceEmbeddings (same BAAI/bge-small-en-v1.5
model as the framework backend, so embeddings are comparable).

NOTE: stores are NOT interchangeable with the framework backend — this
backend keeps its own files in the store dir (faiss_index/ via FAISS
save_local/load_local, entries.jsonl for metadata); switching
EAX_MEMORY_BACKEND starts a fresh memory for existing stores.
"""

import hashlib
import json
import os
import time
import uuid
from pathlib import Path

# FAISS's multithreaded OpenMP segfaults on macOS once torch is loaded — pin
# FAISS to a single thread before any index operation (same as ltm.py).
os.environ.setdefault("OMP_NUM_THREADS", "1")
try:
    import faiss

    faiss.omp_set_num_threads(1)
except ImportError:  # pragma: no cover
    pass

from .ltm import EMBEDDING_MODEL, unquote_content  # noqa: F401  (re-export)

_INDEX_DIR = "faiss_index"
_ENTRIES_FILE = "entries.jsonl"


def _embeddings():
    from langchain_huggingface import HuggingFaceEmbeddings

    return HuggingFaceEmbeddings(
        model_name=EMBEDDING_MODEL,
        model_kwargs={"device": "cpu"},
        encode_kwargs={"normalize_embeddings": True},
    )


def _content_of(message) -> str:
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    if content is None:
        content = str(message)
    return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)


class LCLongTermMemory:
    """LangChain-backed per-agent long-term memory."""

    def __init__(self, store_dir, corpus_id: str):
        self.store_dir = Path(store_dir)
        self.corpus_id = corpus_id
        self._embed = None  # lazy: sentence-transformers load is expensive
        self._vs = None
        self._entries: list[dict] = []
        self._hashes: set[str] = set()
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.load()

    # -- persistence --------------------------------------------------------

    def save(self, *_args, **_kwargs) -> None:
        if self._vs is not None:
            # allow_dangerous_deserialization is required by LangChain to
            # reload a local FAISS index; the index is written by us into the
            # local store dir, so this is the intended use.
            self._vs.save_local(str(self.store_dir / _INDEX_DIR))
        with open(self.store_dir / _ENTRIES_FILE, "w", encoding="utf-8") as f:
            for entry in self._entries:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    def load(self, *_args, **_kwargs) -> list:
        entries_path = self.store_dir / _ENTRIES_FILE
        if entries_path.is_file():
            with open(entries_path, encoding="utf-8") as f:
                self._entries = [json.loads(line) for line in f if line.strip()]
            self._hashes = {e["content_hash"] for e in self._entries}
        index_path = self.store_dir / _INDEX_DIR
        if index_path.is_dir():
            from langchain_community.vectorstores import FAISS

            self._vs = FAISS.load_local(
                str(index_path), self._get_embed(),
                allow_dangerous_deserialization=True,  # see save()
            )
        return [e["memory_id"] for e in self._entries]

    def _get_embed(self):
        if self._embed is None:
            self._embed = _embeddings()
        return self._embed

    # -- mutation / retrieval ------------------------------------------------

    def add(self, messages) -> list[str]:
        if not isinstance(messages, list):
            messages = [messages]
        memory_ids = []
        for message in messages:
            content = _content_of(message)
            if not content:
                continue
            content_hash = hashlib.sha256(content.encode()).hexdigest()
            if content_hash in self._hashes:
                continue  # dedup, same as the framework backend
            memory_id = str(uuid.uuid4())
            entry = {
                "memory_id": memory_id,
                "content": content,
                "content_hash": content_hash,
                "timestamp": getattr(message, "timestamp", None)
                or time.strftime("%Y-%m-%d %H:%M:%S"),
                "agent": getattr(message, "agent", None),
                "wf_task": getattr(message, "wf_task", None),
                "wf_goal": getattr(message, "wf_goal", None),
                "msg_type": str(getattr(message, "msg_type", "") or ""),
            }
            if self._vs is None:
                from langchain_community.vectorstores import FAISS

                self._vs = FAISS.from_texts(
                    [content], self._get_embed(),
                    metadatas=[{"memory_id": memory_id}], ids=[memory_id],
                )
            else:
                self._vs.add_texts(
                    [content], metadatas=[{"memory_id": memory_id}], ids=[memory_id]
                )
            self._entries.append(entry)
            self._hashes.add(content_hash)
            memory_ids.append(memory_id)
        return memory_ids

    def search(self, query, n: int | None = None, **_kwargs):
        """Vector search; returns [(evoagentx Message, memory_id)] like the
        framework backend."""
        if self._vs is None:
            return []
        from evoagentx.core.message import Message

        docs = self._vs.similarity_search(str(query), k=n or 5)
        by_id = {e["memory_id"]: e for e in self._entries}
        hits = []
        for doc in docs:
            entry = by_id.get(doc.metadata.get("memory_id"))
            if entry is None:
                continue
            hits.append((
                Message(
                    content=entry["content"],
                    timestamp=entry.get("timestamp"),
                    agent=entry.get("agent"),
                    wf_task=entry.get("wf_task"),
                    wf_goal=entry.get("wf_goal"),
                ),
                entry["memory_id"],
            ))
        return hits


def open_memory(store_dir, corpus_id: str, create: bool = False):
    """Open the LangChain-backed LTM at store_dir; None if absent and not create."""
    path = Path(store_dir)
    if not create and not (path / _ENTRIES_FILE).is_file():
        return None
    return LCLongTermMemory(path, corpus_id)


def list_entries(store_dir) -> list[dict]:
    """All stored entries (this backend's entries.jsonl)."""
    path = Path(store_dir) / _ENTRIES_FILE
    if not path.is_file():
        return []
    with open(path, encoding="utf-8") as f:
        entries = [json.loads(line) for line in f if line.strip()]
    for entry in entries:
        entry.pop("content_hash", None)
    entries.sort(key=lambda e: e.get("timestamp") or "")
    return entries
