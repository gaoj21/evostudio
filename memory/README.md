# memory layer

Short-term and long-term memory for the project.

## Files

- `stm.py` — `ShortTermMemory`: process-local, session-bucketed working
  memory (`append` / `get` / `clear`), optional JSON persistence.
- `ltm.py` — long-term memory, **framework backend** (default): evoagentx
  `LongTermMemory` (SQLite + FAISS, local bge-small-en-v1.5 embeddings).
- `ltm_langchain.py` — long-term memory, **LangChain backend**:
  `langchain_community` FAISS vectorstore + `langchain_huggingface`
  `HuggingFaceEmbeddings` (same embedding model).

## LTM interface (both backends)

```python
from memory import open_memory, list_entries, unquote_content

mem = open_memory(store_dir, corpus_id, create=True)  # None if absent and not create
mem.add([message])          # framework Message / dict / str
mem.save()                  # persist
mem.load()                  # reload from store
mem.search(query, n=5)      # -> [(Message, memory_id)]
list_entries(store_dir)     # -> [{memory_id, content, timestamp, ...}]
```

## Switching backends

Set `EAX_MEMORY_BACKEND=framework` (default) or `=langchain` before process
start. `memory.open_memory` / `list_entries` dispatch on it; Studio's
`studio/backend/memory_store.py` goes through the same dispatcher.

NOTE: the two backends' on-disk stores are **not interchangeable** —
framework keeps a corpus dump in `memory.db`, LangChain keeps
`faiss_index/` + `entries.jsonl`. Switching backends starts a fresh memory
for existing stores.
