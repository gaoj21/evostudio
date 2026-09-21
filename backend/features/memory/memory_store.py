"""Studio layout over the memory layer (memory/ltm.py).

Each (graph_id, agent/task name) pair gets an isolated store under
backend/data/memory/<graph_id>/<agent_name>/ (SQLite + FAISS), with the
deterministic corpus id `studio-<graph_id>-<agent>`. All LTM machinery lives
in the top-level `memory` package; this module only owns Studio's directory
and corpus-id conventions.
"""

from pathlib import Path
from .identity import storage_name, display_names
from backend.api.studio_config import data_path

from memory import _backend  # dispatch on EAX_MEMORY_BACKEND
from memory.ltm import (  # noqa: F401  (re-exported for existing imports)
    DEFAULT_TOP_K,
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    unquote_content,
)

MEMORY_DIR = data_path("memory")


def store_dir(graph_id: str, agent: str) -> Path:
    return MEMORY_DIR / graph_id / storage_name(graph_id, agent)


def corpus_id(graph_id: str, agent: str) -> str:
    return f"studio-{graph_id}-{storage_name(graph_id, agent)}"


def list_agents(graph_id: str) -> list[str]:
    base = MEMORY_DIR / graph_id
    if not base.is_dir():
        return []
    return display_names(graph_id, [p.name for p in base.iterdir() if p.is_dir()])


def open_memory(graph_id: str, agent: str, create: bool = False):
    """Open the LongTermMemory for a graph/agent store, loading saved data.

    Returns None when the store does not exist and create=False.
    """
    return _backend().open_memory(
        store_dir(graph_id, agent), corpus_id(graph_id, agent), create=create
    )


def list_entries(graph_id: str, agent: str) -> list[dict]:
    """All stored memory entries for a graph/agent store."""
    return _backend().list_entries(store_dir(graph_id, agent))
