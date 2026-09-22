"""Memory APIs, with RAG-backed components loaded only when requested."""

from __future__ import annotations

from importlib import import_module

from .memory import BaseMemory, ShortTermMemory

__all__ = ["BaseMemory", "ShortTermMemory", "LongTermMemory", "MemoryManager"]

_EXPORTS = {
    "LongTermMemory": (".long_term_memory", "LongTermMemory"),
    "MemoryManager": (".memory_manager", "MemoryManager"),
}


def __getattr__(name: str):
    export = _EXPORTS.get(name)
    if export is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, attribute = export
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
