"""memory layer — short-term and long-term memory for the project.

- stm: process-local session-bucketed working memory — ShortTermMemory.
- ltm: persistent per-agent long-term memory with two interchangeable
  backends, selected by the EAX_MEMORY_BACKEND env var:
    - "framework" (default): evoagentx LongTermMemory (FAISS + SQLite)
    - "langchain": langchain_community FAISS + HuggingFaceEmbeddings
      (see ltm_langchain.py; stores are NOT interchangeable)

  Both expose open_memory(store_dir, corpus_id, create=False) returning an
  object with add(messages) / search(query, n) / save() / load(), plus
  list_entries(store_dir) and unquote_content(text).
"""

import os

from .stm import ShortTermMemory

BACKEND_ENV = "EAX_MEMORY_BACKEND"
DEFAULT_BACKEND = "framework"


def _backend():
    name = os.getenv(BACKEND_ENV, DEFAULT_BACKEND)
    if name == "langchain":
        from . import ltm_langchain

        return ltm_langchain
    if name == "framework":
        from . import ltm

        return ltm
    raise ValueError(
        f"Unknown {BACKEND_ENV}={name!r} (expected 'framework' or 'langchain')"
    )


def open_memory(store_dir, corpus_id, create: bool = False):
    return _backend().open_memory(store_dir, corpus_id, create=create)


def list_entries(store_dir):
    return _backend().list_entries(store_dir)


def unquote_content(content):
    return _backend().unquote_content(content)


# Backend-agnostic constants (embedding config shared by both backends).
from .ltm import DEFAULT_TOP_K, EMBEDDING_DIM, EMBEDDING_MODEL  # noqa: E402

__all__ = [
    "open_memory",
    "list_entries",
    "unquote_content",
    "ShortTermMemory",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIM",
    "DEFAULT_TOP_K",
    "BACKEND_ENV",
    "DEFAULT_BACKEND",
]
