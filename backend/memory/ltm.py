"""Long-term memory for the memory layer.

Moved up from backend/api/memory_store.py and generalized: the caller
supplies the store directory and a deterministic corpus id (required —
LongTermMemory.save/load are keyed by corpus_id, so a random one, the
framework default, would make stores unloadable across processes).

Notes baked in from production use:
- FAISS's multithreaded OpenMP segfaults on macOS once torch (via the
  sentence-transformers embedding) is loaded in the same process — pin FAISS
  to a single thread before any index operation.
- BAAI/bge-small-en-v1.5 is 384-dimensional (not 768).
"""

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "1")
try:
    import faiss

    faiss.omp_set_num_threads(1)
except ImportError:  # pragma: no cover - faiss is a framework dependency
    pass

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

EMBEDDING_MODEL_ID = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIM = 384
# Where a copy of the model is looked for, in order: EAX_EMBEDDING_MODEL (a
# folder), then models/bge-small-en-v1.5 in the project. Machines without
# Hugging Face access unzip the model there; nothing is fetched at run time.
LOCAL_MODEL_DIRS = (_REPO_ROOT / "models" / "bge-small-en-v1.5",)


def embedding_model() -> str:
    """The embedding model to load: a local folder when one is present,
    else the Hugging Face id (resolved from its cache or downloaded)."""
    import os
    configured = os.environ.get("EAX_EMBEDDING_MODEL")
    candidates = ([Path(configured).expanduser()] if configured else []) + list(LOCAL_MODEL_DIRS)
    for folder in candidates:
        if (folder / "config.json").is_file() and any(
                (folder / name).is_file() for name in ("model.safetensors", "pytorch_model.bin")):
            return str(folder)
    return EMBEDDING_MODEL_ID


EMBEDDING_MODEL = embedding_model()
DEFAULT_TOP_K = 5


def _make_storage_handler(path: Path):
    from evoagentx.storages.base import StorageHandler
    from evoagentx.storages.storages_config import DBConfig, StoreConfig, VectorStoreConfig

    path.mkdir(parents=True, exist_ok=True)
    store_config = StoreConfig(
        dbConfig=DBConfig(db_name="sqlite", path=str(path / "memory.db")),
        vectorConfig=VectorStoreConfig(
            vector_name="faiss", dimensions=EMBEDDING_DIM, index_type="flat_l2"
        ),
        path=str(path / "index"),
    )
    return StorageHandler(storageConfig=store_config)


def _make_rag_config():
    from evoagentx.rag.rag_config import (
        ChunkerConfig,
        EmbeddingConfig,
        IndexConfig,
        RAGConfig,
        ReaderConfig,
        RetrievalConfig,
    )

    return RAGConfig(
        reader=ReaderConfig(recursive=False, exclude_hidden=True),
        chunker=ChunkerConfig(strategy="simple", chunk_size=512, chunk_overlap=0),
        embedding=EmbeddingConfig(
            provider="huggingface", model_name=EMBEDDING_MODEL, device="cpu"
        ),
        index=IndexConfig(index_type="vector"),
        retrieval=RetrievalConfig(
            retrivel_type="vector",
            postprocessor_type="simple",
            top_k=DEFAULT_TOP_K,
            similarity_cutoff=0.3,
        ),
    )


def open_memory(store_dir, corpus_id: str, create: bool = False):
    """Open the framework LongTermMemory persisted at store_dir.

    Loads existing data (rebuilds the FAISS index from the SQLite memory
    table). Returns None when the store does not exist and create=False.
    """
    path = Path(store_dir)
    if not create and not (path / "memory.db").is_file():
        return None

    from evoagentx.memory.long_term_memory import LongTermMemory

    memory = LongTermMemory(
        storage_handler=_make_storage_handler(path),
        rag_config=_make_rag_config(),
        default_corpus_id=corpus_id,
    )
    if (path / "memory.db").is_file():
        memory.load()
    return memory


def unquote_content(content):
    """The framework json.dumps message.content into chunk metadata; unwrap
    one level of encoding so display shows the raw payload."""
    if isinstance(content, str):
        try:
            decoded = json.loads(content)
            if isinstance(decoded, str):
                return decoded
        except (json.JSONDecodeError, TypeError):
            pass
    return content


def list_entries(store_dir) -> list[dict]:
    """All stored memory entries, read straight from the SQLite memory table."""
    path = Path(store_dir)
    if not (path / "memory.db").is_file():
        return []
    handler = _make_storage_handler(path)
    records = handler.load(tables=["memory"]).get("memory", [])
    entries = []
    for record in records:
        entries.extend(_record_to_entries(record))
    entries.sort(key=lambda e: e.get("timestamp") or "")
    return entries


def _record_to_entries(record: dict) -> list[dict]:
    """One memory-table row (corpus dump) -> one entry per chunk."""
    content = record.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            return []
    chunks = (content or {}).get("chunks", [])
    entries = []
    for chunk in chunks:
        meta = chunk.get("metadata") or {}
        entries.append({
            "memory_id": meta.get("memory_id") or chunk.get("chunk_id"),
            "content": unquote_content(meta.get("content") or chunk.get("text") or ""),
            "timestamp": meta.get("timestamp"),
            "agent": meta.get("agent"),
            "wf_task": meta.get("wf_task"),
            "wf_goal": meta.get("wf_goal"),
            "msg_type": meta.get("msg_type"),
        })
    return entries
