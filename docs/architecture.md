# Architecture

The repository is a small monorepo with a framework at its center and two
applications around it. Dependencies point inward toward `evoagentx`; the
memory adapters build on the framework rather than sitting below it. The `llm`
package is the exception and sits beside everything: it depends on nothing in
this repository, and everything that makes a model call depends on it.

```text
frontend/                  React UI
backend/api/               Platform API and execution
backend/evoagentx/         Agent/workflow framework
llm/                       The model boundary (standalone package, repo root)
backend/features/model_bridge.py   Adapts `llm` to the framework's model classes
backend/memory/            Storage adapters
projects/credit_risk/      Credit-risk domain project
```

Data flow: the GUI builds and runs workflow graphs; the App assembles
domain pipelines; both execute on the EvoAgentX framework, which calls LLMs
through the `llm` package and persists memory through the `memory` layer.

## Layers

### llm/ — the model boundary

A standalone package at the repository root. **Nothing in `backend/` or
`frontend/` may name a provider, import a provider SDK, or read a provider's
response shape** — that is the whole point of the package, and it is what lets
the same checkout run against a different transport elsewhere (on the user's
other machine this package is SafeChain-backed, provider name `safechain`, and
no other file differs).

Several providers can be registered at once in `llm/providers.json`; each may
use a different transport (`type: "litellm"` goes through the litellm SDK,
`type: "openai_compatible"` POSTs to `{base_url}/chat/completions` directly,
`adapter` names a module of your own). API keys are read from the environment
(repo-root `.env`), never stored in the config.

The public API, all importable from the top-level `llm`: `chat`, `chat_result`,
`batch`, `batch_result`, their `a`-prefixed async forms, `LLMResult`,
`LLMUsage`, `UsageTracker`, `list_providers`, `get_provider`,
`default_provider`, `ProviderError`. Callers import from `llm`, never from
`llm.client`, `llm.registry` or `llm.adapters`.

**Switch provider:** change `default` in `llm/providers.json`, or set
`LLM_PROVIDER` (Studio also honours `EAX_PROVIDER`, which wins). **Add one:**
an entry in `llm/providers.json` plus, for a new transport, a module in
`llm/adapters/` offering `complete` (optionally `acomplete`, `native_batch`).
Smoke test with `python llm/examples_terminal.py` in the project's environment (`./start.sh` picks it: EVO_VENV, else the `evo` conda env, else `.venv`). See
[llm-contract.md](llm-contract.md). 该包不在仓库内，每台机器自备实现。

### backend/features/model_bridge.py — `llm` as a framework model

The framework instantiates a model class from an `LLMConfig`, and the Deep
Agents harness wants a LangChain chat model; neither is text in, text out. The
bridge is the one place that adapts: `workflow_model()` returns the registered
`StudioLLM`, `agent_model()` a LangChain `BaseChatModel` with `usage_metadata`
filled from `LLMResult.usage`, and `usage_hook()` is where a run's token
accounting comes from. Token usage is never read off a provider response.

### backend/memory/ — memory adapter

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
  memory for existing stores. See `backend/memory/README.md`.
- `memory.stm` — `ShortTermMemory`: process-local, session-bucketed working
  memory (`append` / `get` / `clear`), optional JSON persistence.

Consumers: Studio keeps only its directory/corpus-id conventions in
`backend/api/memory_store.py` (thin shim); apps pass their own store root.

### evoagentx/ — framework

Agent framework (workflow graphs, agents, tools, RAG, optimizers). This is an
actively modified package and the stable dependency boundary for applications.

### projects/credit_risk/ — application

The credit-risk monitoring app: agentic pipeline (`agentic_pipeline/`),
dataset builders (`dataset/`), evaluation and optimization scripts. LLM
construction goes through `model_bridge.workflow_model()`.

### backend/api/ and frontend/ — GUI

EvoAgentX Studio: FastAPI backend (`backend/api/`) + React canvas
(`frontend/`). Graph CRUD, background runs, three node kinds (LLM
task / `kind="source"` input source / `kind="tool"` deterministic tool
node), per-node LTM, batch runs, watch triggers, MIPRO prompt optimization,
HITL review, and a per-graph workspace (run artifacts + user-managed files:
upload / create / edit / delete). Stable core and experimental integrations
are separated in `studio/README.md`; see `studio/API.md` for the REST contract.
