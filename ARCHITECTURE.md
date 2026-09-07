# Architecture

The repository is a small monorepo with a framework at its center and two
applications around it. Dependencies point inward toward `evoagentx`; the LLM
and memory adapters build on the framework rather than sitting below it.

```
┌─────────────────────────────────────────────────────┐
│ Applications  studio/          visual workflow       │
│               credit_risk/     domain application    │
├─────────────────────────────────────────────────────┤
│ Adapters      llm/             provider registry     │
│               memory/          persistence backends  │
├─────────────────────────────────────────────────────┤
│ Framework     evoagentx/       agents, workflows,     │
│                                models, tools and RAG   │
└─────────────────────────────────────────────────────┘
```

Data flow: the GUI builds and runs workflow graphs; the App assembles
domain pipelines; both execute on the EvoAgentX framework, which calls LLMs
through the `llm` layer and persists memory through the `memory` layer.

## Layers

### llm/ — LLM provider adapter

Several providers can be registered at once in `llm/providers.json`; each may
use a different calling convention (`type: "litellm"` goes through the
litellm SDK, `type: "openai_compatible"` POSTs to `{base_url}/chat/completions`
directly). API keys are read from the environment (repo-root `.env`), never
stored in the config.

- `llm.registry` — `list_providers()`, `get_provider(name)` (default from
  config; resolves the key, clear errors for unknown/disabled providers).
- `llm.client` — `chat(provider, messages, **kwargs) -> str` with timeout and
  429/5xx retry.
- `llm.factory` — `get_evoagentx_llm(provider=None)` builds a framework
  `LiteLLM` instance.

**Add a provider:** add an entry to `llm/providers.json` (name, type, model,
`api_key_env`, optional `base_url`/`params`), put the key in `.env`. A new
calling convention = a new `type` with a handler in `client.py` and a branch
in `factory.py`. See `llm/README.md`.

### memory/ — memory adapter

- `memory.ltm` — persistent per-agent long-term memory, **framework
  backend** (default): framework `LongTermMemory` (SQLite + FAISS, local
  bge-small embeddings). Includes the macOS FAISS/torch single-thread
  workaround and the correct 384-dim embedding config.
- `memory.ltm_langchain` — **LangChain backend**: `langchain_community`
  FAISS vectorstore + `langchain_huggingface` embeddings (same model).
  Switch with `EAX_MEMORY_BACKEND=framework|langchain` (default framework).
  Both backends expose the same interface — `open_memory(store_dir,
  corpus_id, create=False)` returning an object with `add(messages)` /
  `search(query, n)` / `save()` / `load()`, plus `list_entries(store_dir)`
  and `unquote_content(text)`. The two on-disk formats are **not
  interchangeable** (framework: corpus dump in `memory.db`; LangChain:
  `faiss_index/` + `entries.jsonl`) — switching backends starts a fresh
  memory for existing stores. See `memory/README.md`.
- `memory.stm` — `ShortTermMemory`: process-local, session-bucketed working
  memory (`append` / `get` / `clear`), optional JSON persistence.

Consumers: Studio keeps only its directory/corpus-id conventions in
`studio/backend/memory_store.py` (thin shim); apps pass their own store root.

### evoagentx/ — framework

Agent framework (workflow graphs, agents, tools, RAG, optimizers). This is an
actively modified package and the stable dependency boundary for applications.

### credit_risk/ — application

The credit-risk monitoring app: agentic pipeline (`agentic_pipeline/`),
dataset builders (`dataset/`), evaluation and optimization scripts. LLM
construction goes through `get_evoagentx_llm()`.

### studio/ — GUI

EvoAgentX Studio: FastAPI backend (`studio/backend/`) + React canvas
(`studio/frontend/`). Graph CRUD, background runs, three node kinds (LLM
task / `kind="source"` input source / `kind="tool"` deterministic tool
node), per-node LTM, batch runs, watch triggers, MIPRO prompt optimization,
HITL review, and a per-graph workspace (run artifacts + user-managed files:
upload / create / edit / delete). Stable core and experimental integrations
are separated in `studio/README.md`; see `studio/API.md` for the REST contract.
