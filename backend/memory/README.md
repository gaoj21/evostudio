# memory layer

Short-term and long-term memory for the project.

## Files

- `stm.py` — `ShortTermMemory`: process-local, session-bucketed working
  memory (`append` / `get` / `clear`), optional JSON persistence.
- `ltm.py` — long-term memory, **framework backend** (default): evoagentx
  `LongTermMemory` (SQLite + FAISS, local bge-small-en-v1.5 embeddings).
- `ltm_langgraph.py` — long-term memory, **LangGraph backend**: a LangGraph
  `SqliteStore` with a vector index (sqlite-vec), one `store.sqlite` per
  store, the same embedding model; writes commit immediately.

## LTM interface (both backends)

```python
from memory import open_memory, list_entries, unquote_content

mem = open_memory(store_dir, corpus_id, create=True)  # None if absent and not create
mem.add([message])          # framework Message / dict / str
mem.save()                  # persist
mem.load()                  # reload from store
mem.search(query, n=5)      # -> [(Message, memory_id)]
mem.delete([memory_id])     # -> [bool]
list_entries(store_dir)     # -> [{memory_id, content, timestamp, ...}]
```

## Switching backends

Set `EAX_MEMORY_BACKEND=framework` (default) or `=langgraph` before process
start. `memory.open_memory` / `list_entries` dispatch on it; Studio's
`backend/api/memory_store.py` goes through the same dispatcher.

NOTE: the two backends' on-disk stores are **not interchangeable** —
framework keeps a corpus dump in `memory.db`, LangGraph keeps
`store.sqlite`. Switching backends starts a fresh memory
for existing stores.
