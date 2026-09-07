# Studio API Contract (v1, MVP)

Backend: FastAPI at `studio/backend/`, served with uvicorn on port 8000.
Frontend: Vite + React + @xyflow/react at `studio/frontend/`, dev server
proxies `/api` to :8000; production build is statically hosted by FastAPI.

All JSON. Graph ids are URL-safe slugs (`re.sub(r'[^a-z0-9-]', ...)`).

## Data model — canvas graph

```jsonc
{
  "id": "my-flow",                 // assigned by backend
  "name": "My Flow",
  "goal": "what this workflow achieves",
  "tasks": [                       // canvas nodes, in no required order
    {
      "name": "node_a",            // unique per graph
      "description": "...",
      "inputs":  [{"name": "topic", "type": "str", "description": "...", "required": true}],
      "outputs": [{"name": "result", "type": "str", "description": "...", "required": true}],
      "prompt": "...",             // may reference inputs as {topic}
      "system_prompt": "...",      // optional
      "parse_mode": "str",         // "str" | "json" | "title" | "xml"
      "use_long_term_memory": false, // optional; enables per-agent LTM (see Memory)
      "tool_names": ["PythonInterpreterToolkit"], // optional; toolkit names from GET /api/tools
      "skill_names": ["risk_rubric"], // optional; skills appended to system_prompt at run time
      "x": 120, "y": 80            // canvas position (UI-only)
    }
  ],
  "edges": [{"source": "node_a", "target": "node_b"}]  // task names
}
```

Semantics: data flows by NAME — a task input is satisfied by an upstream
task's output with the same name, or by a workflow input with that name.
Edges define execution order (backend topologically sorts; sequential
execution). Unsourced inputs become workflow inputs (asked at run time).

## Endpoints

- `GET /api/health` → `{"ok": true}`
- `GET /api/palette` → `{"templates": [{type, label, description, defaults: {...task fields...}}]}`
- `GET /api/graphs` → `[{id, name, goal, updated_at}]`
- `POST /api/graphs` `{name, goal}` → full graph (empty tasks)
- `GET /api/graphs/{id}` → canvas graph JSON, plus computed
  `workflow_inputs: [{name,type,description,required,consumed_by}]`
  (`consumed_by` is the first task needing the value, so the run form can say
  what each input is for). Values are sent to `/run` **in their declared type**:
  the framework validates them against the JSON-schema type, and a `list`/`dict`
  supplied as a JSON *string* is rejected.
- `PUT /api/graphs/{id}` (body: canvas graph) → saved graph +
  `workflow_inputs`. On framework validation failure: HTTP 422
  `{"detail": [error strings]}`.
- `DELETE /api/graphs/{id}` → `{"ok": true}`
- `POST /api/graphs/{id}/run` `{inputs: {name: value}}` → `{run_id}`
  (starts a background thread)
- `GET /api/runs/{run_id}` →
  ```jsonc
  {
    "run_id": "...", "graph_id": "...", "status": "running|success|failed",
    "error": null,
    "nodes": [{"name": "node_a", "status": "pending|running|completed|failed",
               "output": {"result": "..."} | null}],
    "result": null | {...}          // WorkflowResult.result when done
  }
  ```
- `GET /api/runs?graph_id=...` → recent runs (newest first, cap 20)

### Evaluation

An evaluation is a batch run with a metric attached — the same records, the
same runner, plus a score per item and an aggregate. Reusing the batch path
keeps node progress, the result drawer, review routing and artifacts working
during an evaluation.

- `GET /api/metrics` → `{"metrics": [{name, description, custom}]}`. The
  built-ins are the optimizer's own (`exact_match`, `contains`, `numeric`,
  `credit_risk`), so a workflow scores the same way whether it is evaluated or
  optimized. A **custom metric** is any custom tool whose params are exactly
  `prediction` and `label`; it may return a number, a bool, or an object with a
  `score` key plus whatever else you want recorded per item.

- `POST /api/graphs/{graph_id}/run-batch` accepts two more fields (multipart or
  JSON): `metric` and `label_key`. With them the batch becomes an evaluation.

  `label_key` names the field holding the expected answer. It is **removed from
  each record before the run**, so a node that happens to declare an input of
  that name is never handed the answer. A record missing the field is a 422
  naming the record and the fields it does have.

The batch document then carries `metric`, a per-item `score` / `score_detail` /
`label`, and a `summary`:

```jsonc
{"scored": 3, "total": 3, "unscored": 0,
 "mean": 1.0, "min": 1.0, "max": 1.0, "perfect": 3}
```

A metric that raises marks that one item unscored with the reason and leaves
the rest of the batch alone — the runs themselves succeeded and are worth
keeping, and `unscored` reports how many did not get a number.

### Starting part-way through

- `GET /api/graphs/{graph_id}/inputs?start_at=<name>[,<name>]` → what a run
  would ask for if it began at those nodes:
  ```jsonc
  {"start_at": ["detect"],
   "nodes": ["feed", "source_news", ...],        // every node, for the picker
   "workflow_inputs": [{name, type, description, required, consumed_by}],
   "prefill": {"grounding": "..."},              // taken from run history
   "prefill_from_run": "a77a303e93d8",
   "prefill_missing": []}                        // what no past run could supply
  ```
  Omit `start_at` for the whole workflow. An unknown node name is 422.

- `POST /api/graphs/{graph_id}/run` accepts `"start_at": ["detect"]`. Everything
  upstream of those nodes is left out of the run, so what they would have
  produced has to arrive as an input — normally the outputs of an earlier run,
  which is what `prefill` is for.

  `prefill` prefers the most recent run that covers **every** wanted input, not
  merely the most recent run holding some of them: the newest run is usually
  the failed one being re-run, and it stopped before producing the later
  values.

### Preprocessing inputs

A workflow may name a custom tool in `"preprocess"`. Every input record goes
through it before the workflow sees it — each record of a batch, and the inputs
dict of a single run (passed as one record, so the same tool serves both).

The tool takes exactly one object parameter and returns an object:
`def run(record): return {...}`. For a batch it runs **before** records are
matched to workflow inputs, which is what lets it rename or derive the very
fields the mapping needs.

A preprocessor that is missing, takes the wrong number of parameters, fails, or
returns a non-object refuses the run with 422 — feeding the workflow
unprocessed data would produce results that look right and are wrong.

`preprocess` is part of the graph document, so it is saved, exported and
imported with the workflow.

### Batch runs

- `POST /api/graphs/{id}/run-batch` → `{"batch_id", "total"}` (runs records
  concurrently — ThreadPoolExecutor, `workers` field 1-5, default 2 — each
  record is a normal run).
  Two body shapes:
  - `multipart/form-data` with a `file` field: `.jsonl` (one JSON object per
    line) or `.csv` (column names map to input names); optional form fields
    `workers`, `review_zone`.
  - JSON `{"source": "credit_risk"|"canvas", "split"?, "n"?, "seed"?, "workers"?}` —
    `credit_risk` samples `credit_risk/dataset/contemporary/samples.jsonl`
    (`n: 0` runs the entire split in file order, no sampling);
    `canvas` uses the graph's connected credit_risk source node config.
  - Mapping: a record fills a workflow input when names match; a missing
    required input fails the whole request with HTTP 422 listing the
    available record fields. Extra record fields are ignored.
  - Concurrent items of one graph share LTM stores; runner serializes all
    LTM open/search/add/save with per-store locks.
- `GET /api/batches/{batch_id}` →
  ```jsonc
  {
    "batch_id": "...", "graph_id": "...", "status": "running|completed",
    "source": {"type": "upload", ...} | {"type": "credit_risk", ...}
              | {"type": "canvas", "node": "feed", ...},
    "total": 3,
    "items": [{"index": 0, "status": "pending|running|success|failed",
               "run_id": "...", "inputs": {...},
               "output_summary": "...(≤500 chars)", "error": null}],
    // aggregate per-node status counts across item runs (pending items, or
    // items whose run state is unavailable, count all nodes as pending);
    // powers the canvas batch progress badges
    "node_progress": {"node_a": {"completed": 2, "running": 0, "failed": 1, "pending": 0}}
  }
  ```
- `GET /api/sources/credit-risk` → `{id, label, splits: {train: N, ...},
  fields: [...]}` (dataset availability for the batch dialog).

### Memory (long-term)

Tasks with `use_long_term_memory: true` get an isolated store at
`studio/data/memory/<graph_id>/<task_name>/` (SQLite + FAISS, corpus id
`studio-<graph_id>-<task_name>`; embedding: local HF `BAAI/bge-small-en-v1.5`).
Before execution, relevant past memories are appended to the task prompt;
after a successful run, the node's inputs/outputs are stored. Memory failures
never fail the run — they surface as `memory_error` on the run object.

- `GET /api/graphs/{id}/memory/agents` → `{"agents": [task names with stores]}`
- `GET /api/graphs/{id}/memory?agent=<task>&q=<query>&n=10` →
  ```jsonc
  {"agent": "...", "query": "..." | null,
   "entries": [{"memory_id", "content", "timestamp", "agent",
                "wf_task", "wf_goal", "msg_type"}]}
  ```
  Without `q`, lists all entries (raw SQLite memory table); with `q`, runs a
  vector search (top-`n`).

### Evolve (MIPRO prompt optimization)

Optimizes node prompts with the framework's `WorkFlowMiproOptimizer`
(canvas prompts become `MiproPromptTemplate` instructions; dspy 3.3.0
compat shims from `credit_risk/optimize_mipro.py` are applied in
`evolve_api.py`). Zero-shot by default (`max_*_demos=0`). Artifacts per
task at `studio/data/evolve/<task_id>/`: `dataset.jsonl` (resolved eval
records), `best_program.json` (framework graph save), `result.json`.

- `GET /api/evolve/metrics` → `{"metrics": [{name, description}]}` —
  built-ins: `exact_match`, `contains`, `numeric`, `credit_risk`
  (risk_level rule over `label.type` positive/negative).
- `POST /api/graphs/{id}/evolve` → `{"task_id"}`. Two body shapes:
  - `multipart/form-data`: `file` = JSONL of `{"inputs": {...}, "label": ...}`
    (flat records also accepted — all keys except `label`/`id` become inputs),
    plus form fields `metric`, `n_train`, `n_dev`, `num_candidates`,
    `max_steps`, `seed`.
  - JSON `{"source": "credit_risk", "split"?, "n"?, "seed"?, "metric", ...}`
    — samples with labels from `credit_risk/dataset/contemporary/samples.jsonl`
    (`label = {"type": "positive"|"negative", "event": ...}`).
  - Every record's inputs must cover the graph's required workflow inputs
    (HTTP 422 otherwise). Budget defaults: n_train = records-1, n_dev = 1,
    num_candidates = 2, max_steps = 2 (keep small — each trial re-runs the
    dev set through the LLM).
- `GET /api/evolve?graph_id=...` → recent tasks (summary incl.
  `baseline_score` / `optimized_score`).
- `GET /api/evolve/{task_id}` → full task:
  ```jsonc
  {
    "task_id": "...", "graph_id": "...", "status": "running|done|failed",
    "stage": "baseline|optimizing|evaluating optimized" | null,
    "metric": "exact_match", "params": {...}, "error": null,
    "baseline":  {"metrics": {"score": 0.5}, "records": {"ex1": {prediction, label, metrics}}},
    "optimized": {"metrics": {"score": 1.0}, "records": {...}},
    "diff": [{"name": "node_a", "before": "...", "after": "..."}],
    "optimized_graph": {...canvas graph with prompts replaced...}
  }
  ```
- `POST /api/evolve/{task_id}/apply` → saves `optimized_graph` as a new
  canvas graph named "<name> (evolved)"; 409 unless status is `done`.

### Credit-risk presets & templates

`registry.py` adapts `credit_risk/agentic_pipeline/agents.py` into seven
palette presets — the full flow: `cr_source_news` + `cr_source_8k` (step 1,
entity/CIK extraction) → `cr_grounding` (step 2, uses the
`ObligorMatchToolkit` tools against the internal obligor list) → `cr_detect`
→ `cr_investigate` → `cr_reflect` → `cr_decide` (step 3). Taxonomy/rubric
skill texts are inlined into the prompts (braces escaped); inputs use the
credit_risk feed field names (`company`, `news_batch`, `filing_batch`,
`window_end`). `cr_investigate` and `cr_decide` default
`use_long_term_memory: true` — their per-node LTM stores play the risk
profile and the alert repository (step 5). Note: framework graph validation
treats only in-degree-0 nodes' inputs as workflow inputs, so `source_news`
also declares `company`/`window_end` to expose them as workflow inputs.

The `ObligorMatchToolkit` (`obligor_tool.py`) wraps
`credit_risk/agentic_pipeline/obligors.py`'s `ObligorRegistry` (lazy-loaded
from `credit_risk/dataset/contemporary/candidates.csv`) as tools
`match_company_name` and `match_cik`.

The credit_risk feed (`sources.py`) also projects `filing_batch`: up to 5
filings, each `form` + `items` + first 1500 chars, capped at 6000 chars.

### Canvas source nodes

Nodes with `"kind": "source"` are input sources, not LLM tasks: they are
excluded from the framework's task list (validation + execution), executed
in Python at run start, and their declared `outputs` count as produced for
`workflow_inputs` computation — a graph whose inputs are fully covered by
source nodes needs no manual inputs at run time.

Only source nodes with at least one outgoing edge are executed. An
unconnected source node is inert: the runner skips it (status `skipped`),
it is excluded from validation probes and batch field mapping, and its
`outputs` do not count as produced for `workflow_inputs` (those fields are
asked for as manual inputs instead).

The same holds for task nodes with no edges at all ("parked" draft nodes):
they save without framework validation, never execute (status `skipped`),
and their inputs are excluded from `workflow_inputs` — so alternative nodes
can be parked on the canvas and swapped in by rewiring. The frontend dims
parked nodes. A run with zero connected task nodes fails with a clear
error.

```jsonc
{
  "name": "feed", "kind": "source",
  "source": {"type": "gdelt_news", "query": "Wayfair", "days": 30, "max_records": 10},
  "outputs": [{"name": "company", ...}, ...],   // per-type output names
  "x": -200, "y": 170
}
```

Source types (config schema + output names are served to the frontend via
`GET /api/sources`; the Inspector form is schema-driven):

- `credit_risk` — local dataset sampler (split / n / seed).
- `gdelt_news` (`source_apis.py`) — GDELT doc API artlist (free, no key;
  fair-use 5s spacing + 429/5xx backoff, adapted from
  `credit_risk/dataset/build_r02_fetch_news.py`). Config: query*, days,
  max_records. Outputs: company, news_batch, window_start, window_end,
  n_articles.
- `sec_edgar_8k` — EDGAR submissions + SGML full-text parse (UA header
  mandatory; adapted from `build_r03_fetch_8k.py`). Config: cik*, count.
  Outputs: cik, company, filing_batch, window_end.
- `http_api` — generic JSON API: url with `{placeholders}` filled from
  `vars` (a JSON object in the config), method GET/POST, params/headers/body
  as JSON text; header values of the form `$ENV_VAR` resolve from the
  process environment; `extract` is a dot-path into the response JSON.
  Outputs: api_response (≤8000 chars), status_code.

All HTTP calls use a 30s timeout and up to 5 attempts with backoff
(5/15/30/60s) on 429/5xx; a persistent 429 raises a `SourceError` explaining
the IP hit the API's fair-use cap. Source failures raise `SourceError`: the
run is marked failed and the source node shows status `failed` with
`{"error": ...}` as its output.

- Single run: the first record is merged into the run inputs (explicitly
  provided inputs win); the source node reports `completed` with the record
  (truncated) as its output. Missing/invalid config fails graph save with
  HTTP 422; local dataset problems fail the run request with HTTP 422.
- Batch run: `POST /api/graphs/{id}/run-batch` with `{"source": "canvas"}`
  samples n records using the canvas credit_risk source node's split/n/seed;
  per-record inputs also include the source node's output fields so each
  item gets its own record (API sources produce a single record).

### Watch (scheduled / live sources)

A source node's config may carry a schedule:
`"source": {"type": ..., ..., "schedule": {"mode": "ondemand"|"daily"|"interval",
"time": "09:00", "interval_minutes": 60}}`. `ondemand` (default) = manual runs
only. `watcher.py` runs one daemon thread per scheduled source node; each fire
polls for NEW data (dedup state persisted to
`studio/data/watch/<graph>_<node>.json`) and starts a normal background run per
new record. Interval floor is 5 minutes (GDELT fair use);
`EAX_WATCH_DEBUG=1` lowers it to 0.2 min for testing. Dedup keys:
credit_risk → sample_id (seed advances per poll cycle), gdelt_news → article URL,
sec_edgar_8k → accession number, http_api → response hash.

- `POST /api/graphs/{id}/watch` → start watchers; 422 if the graph has no
  scheduled source node. `{"watching": true, "watchers": [...]}`
- `DELETE /api/graphs/{id}/watch` → stop; `{"watching": false, "stopped": n}`
- `GET /api/graphs/{id}/watch` →
  ```jsonc
  {"watching": true,
   "watchers": [{"node": "feed", "source_type": "gdelt_news",
                 "schedule": {...}, "last_poll": "...", "last_fire": "...",
                 "next_fire": "...", "fired_runs": 2, "last_run_id": "...",
                 "last_error": null}]}
  ```
  Watchers are in-memory only (no auto-restart on server reboot); seen/dedup
  state survives via the JSON files.

- `GET /api/palette` → `templates` now includes the four `cr_*` presets.
- `GET /api/templates` → `[{id, name, description}]`
- `GET /api/templates/{id}` → `{id, name, description, graph: {...canvas graph...}}`

### Tools

`tools_registry.py` catalogs the framework toolkits usable from canvas tasks
(`task.tool_names` — toolkit names; resolved per run via
`tool_names_to_tools`, fresh instances each run). Unknown names or missing
credentials fail the request/run with a clear message; tool execution errors
inside a run surface in the node output / run error.

- `GET /api/tools` →
  ```jsonc
  {"tools": [{"name": "FileToolkit", "description": "...",
              "tools": [{"name": "read_file", "description": "...",
                         "inputs": {"file_path": {"type": "string", ...}},
                         "required": ["file_path"]}],
              "requires": [], "available": true,
              "unavailable_reason": "..."}]}
  ```
  (`tools` is sub-tool granularity with inputs schema — it drives both the
  Inspector attach-select and the draggable tool-node palette.)
- Enabled (zero-config): FileToolkit, StorageToolkit, CMDToolkit,
  PythonInterpreterToolkit, ArxivToolkit, RequestToolkit,
  WikipediaSearchToolkit, DDGSSearchToolkit, RSSToolkit.
- Listed but gated: GoogleSearchToolkit (GOOGLE_API_KEY +
  GOOGLE_SEARCH_ENGINE_ID), SerperAPIToolkit (SERPERAPI_KEY),
  ExaSearchToolkit (EXA_API_KEY). Omitted entirely: browser/playwright,
  docker interpreter, gmail/telegram/google_maps, mongodb/postgresql, MCP,
  image and finance toolkits (heavy deps or credentials).

### Custom tools

User-defined tools live at `studio/data/tools/<name>.json`:
`{name, description, params: [{name, type, description}], code}` where `code`
is Python defining `def run(<params...>): ...` (its JSON-serializable return
value is the tool result). Execution is a subprocess (`.venv/bin/python`,
stdin `{"code", "args"}` → stdout JSON), 30s timeout; failures surface as
`{"error": ...}` in the node's tool result. Trust model: local single-user —
custom code runs with the user's own permissions, no sandboxing.

At run time each custom tool becomes a single-tool framework Toolkit (the
`Tool` subclass is generated with `__call__` params/annotations exactly
matching `params` — the framework validates this at class creation) and is
listed/validated/resolved alongside built-ins. Name conflicts with built-in
toolkit or sub-tool names are rejected.

- `GET /api/tools/custom` → `{"tools": [spec, ...]}`
- `POST /api/tools/custom` (body: spec) → saved spec; HTTP 422 on invalid
  name / param type / uncompilable code / missing `run` / name conflict.
- `DELETE /api/tools/custom/{name}` → `{"ok": true}`

### Skills

A skill is standing instructions a node *follows*, as opposed to a tool, which
is code the workflow *calls*. Stored as `studio/data/skills/<name>/SKILL.md`
in the Agent Skills format (YAML frontmatter + Markdown body) and parsed with
the framework's `evoagentx.skills`, so the same folder works anywhere else in
the project. Overwriting a skill archives the previous body under
`.versions/`.

- `GET /api/skills` → `{"skills": [{name, description, content, resources}]}`
- `GET /api/skills/{name}` → one skill
- `POST /api/skills` `{name, description, content}` → the saved skill.
  422 on an invalid name or an empty description/content.
- `DELETE /api/skills/{name}` → `{"ok": true}`

**Attaching**: an LLM task carries `"skill_names": ["risk_rubric"]`. At run
time each attached skill's body is appended to that node's `system_prompt`
under a `## Skill: <name>` heading, then the node runs as an ordinary task —
no extra LLM call. The canvas keeps `skill_names`; the framework never sees
it. Saving a graph that references an unknown skill fails with 422; a skill
deleted after the fact is skipped at run time rather than breaking the run.

Skills can also be used *on demand* instead of always-on: attach the
`SkillToolkit` toolkit to a node and it gets `list_skills` / `load_skill`,
so the node chooses which skill to read (progressive disclosure).

### Tool nodes (kind="tool")

A tool on the canvas is a deterministic node — no LLM call:
`{"name": "wc", "kind": "tool", "tool": "word_count", "inputs": [...tool
params...], "outputs": [{"name": "result", ...}]}`. `GET /api/tools` lists
every available sub-tool with its inputs schema (built-in toolkits are
introspected; custom tools come from their spec).

Semantics (v1): tool nodes run in Python BEFORE the framework graph, in
topological order — their inputs may come from source nodes, earlier tool
nodes, or workflow inputs only. A tool node whose input depends on an LLM
node's output is rejected at save time with HTTP 422 (attach the tool to the
LLM node via `tool_names` instead). Results land in the node's first output
name (default `result`) and flow downstream by name — non-string results are
JSON-stringified so they satisfy the framework's `str` input validation; the
full structured value is kept in the run's workspace records. Unconnected
tool nodes are parked (skipped), same as other node kinds. Failures fail the run with
the node's error visible.

### Workspace (run artifacts)

Every run writes complete artifacts under `studio/data/workspace/<graph_id>/`:
`<output_dir>/<run_id>/input.json` (effective inputs, source-node records
merged) and `<output_dir>/<run_id>/output.json` (result + every node's FULL
output — the run view truncates source-node outputs, the workspace does not).
`output_dir` is a per-graph setting (default `runs`; set on the graph doc,
validated to stay inside the workspace). Batch items get the same per-run
files. A node with `"save_output": false` is recorded with `output: null`
in output.json (its output still flows to downstream nodes). Storage-backed
toolkits (StorageToolkit / CMDToolkit / PythonInterpreterToolkit) are rooted
at `workspace/<graph_id>/files/` during runs via `LocalStorageHandler`;
FileToolkit paths come from the LLM and are not redirected. Only new runs
write artifacts.

- `GET /api/graphs/{id}/workspace` →
  `{"graph_id", "files": [{path, size, mtime} | {path, dir: true}]}`
- `GET /api/graphs/{id}/workspace/file?path=<relpath>` →
  `{path, size, truncated, content}` (text; >100KB truncated; paths escaping
  the workspace root → 400, missing file → 404).
- `PUT /api/graphs/{id}/workspace/file` `{"path": "files/a.txt", "content": "..."}`
  → create/overwrite a text file (parent dirs auto-created; max 10MB).
- `POST /api/graphs/{id}/workspace/upload[?path=<relpath>]` (multipart `file`)
  → upload any file; defaults to `files/<filename>` when `path` is omitted.
- `POST /api/graphs/{id}/workspace/mkdir` `{"path": "files/batch-1"}` →
  create a directory (parents included).
- `DELETE /api/graphs/{id}/workspace/file?path=<relpath>` → delete a file (or
  an empty directory); missing → 404, non-empty dir → 400.

### HITL review

After a successful run, node outputs are scanned for a decision object
(`{"action": "alert"|"suppress", "score": 0-100, ...}`). An alert with score
in the gray zone (default `[35, 65]`; override per run/batch with
`"review_zone": [lo, hi]` in the run / run-batch body), or any decision with
`"review_required": true`, creates a pending review
(`studio/data/reviews/<id>.json`) and the run gets
`review_status: "awaiting_review"` + `review_id` (batch items mirror
`review_status`). No tkinter — pure web polling.

- `GET /api/review?status=pending` →
  ```jsonc
  [{"review_id", "run_id", "graph_id", "status": "pending|approved|rejected",
    "company", "topic", "severity", "score", "risk_level", "summary",
    "decision": {...}, "zone": [lo, hi], "created_at",
    "resolved_at", "resolution", "final_action", "note"}]
  ```
- `POST /api/review/{review_id}` `{"decision": "approve"|"reject", "note"?}`
  → updated review. approve ⇒ `final_action="alert"`, reject ⇒
  `"suppress"`. The outcome is written back to the run JSON
  (`review_status`/`final_action`/`review_note`) and the batch item.

### Export as a standalone project

- `GET /api/graphs/{graph_id}/export` → a `.zip` (`Content-Disposition:
  attachment`) holding a project that runs the workflow **without Studio**:

  ```
  <graph-id>/
    run.py              CLI: --inputs data/sample_input.json, or per-input flags
    workflow.py         the tasks as Python data + the same execution path
    graph.json          the canvas graph it came from
    manifest.json       provenance: source commit, dirty flag, export time
    tools/              custom tool code and specs it uses
    skills/<name>/SKILL.md
    data/sample_input.json   captured from a real successful run when one exists
    README.md, requirements.txt, .env.example
  ```

  Reproduced faithfully: tool nodes run first and deterministically, their
  structured results are JSON-stringified for downstream LLM tasks, skills are
  appended to system prompts, and the LLM tasks execute as a
  `SequentialWorkFlowGraph`. Only the toolkits and skills the workflow actually
  references are bundled.

  Not reproduced (and stated in the generated README): **source nodes** — their
  feeds live in Studio, so their outputs become declared inputs the caller
  supplies — and **long-term memory** / HITL review. The exported project reads
  `EAX_MODEL` / `EAX_API_KEY` / optional `EAX_BASE_URL` from the environment
  rather than `llm/providers.json`.

### Import a project back

- `POST /api/graphs/import` (multipart, field `file`) accepts either a project
  `.zip` from the export above or a bare `graph.json`, and returns the created
  graph plus `imported_tools`, `imported_skills` and `notes`.

  It **always creates a new workflow** rather than overwriting one — an import
  is usually someone else's copy coming back. Bundled custom tools and skills
  are installed only when the name is free; an existing one is left alone and
  reported in `notes`, so an import cannot quietly rewrite code another
  workflow depends on. An invalid graph is rejected with 422 and the
  placeholder graph is removed.

  Round-trip is lossless: goal, tasks, edges and node positions come back
  identical.

  **`workflow.py` is the source of truth.** When the archive contains one, its
  `TASKS` / `TOOL_NODES` / `EDGES` / `GOAL` decide the workflow — that file is
  what someone edits after taking the project away. It is *parsed, never
  executed* (literal assignments only), so importing a project cannot run its
  code; a file edited into something non-literal falls back to `graph.json`
  with a note. `graph.json` still supplies what code cannot express: canvas
  positions and source nodes. Nodes the code introduced are placed one column
  right of whatever feeds them — an existing hand-made layout is never
  re-flowed. `notes` names every node added, updated (with the fields that
  changed) or removed.

  A bare `workflow.py` can also be uploaded on its own; the canvas is then laid
  out from scratch.

### Run history

- `GET /api/runs?graph_id=<id>` → recent runs, newest first (cap 20), each with
  `run_id`, `status`, `created_at`, `inputs`, `nodes` and `result`. The UI lists
  them and opening one restores it onto the canvas: run mode with every node's
  final status, plus the result drawer for that run.

### Agent endpoint (OpenAI-compatible)

Everything else in this API is push — you trigger a workflow and it produces
something. This is the pull side: anything that speaks the OpenAI chat API can
hold a conversation with an agent that has this Studio's tools, skills and
memory. The agent is not bound to a workflow; each message it decides for
itself whether to answer, call a tool, read a skill, or search memory.

- `GET /v1/models` → one model, `evoagentx-agent`.
- `POST /v1/chat/completions` — the usual body. Honoured fields:
  - `messages` (required), `model`, `stream`
  - **`user`** keys the agent's long-term memory, so a client that sets it gets
    continuity across separate conversations. Stores live in
    `studio/data/agent-memory/<user>/` and are isolated per user.
  - `stream: true` returns one SSE chunk plus `[DONE]` — enough for clients
    that only speak streaming, without pretending the loop emitted tokens
    incrementally.

  ```bash
  curl localhost:8000/v1/chat/completions -H 'Content-Type: application/json' \
    -d '{"model":"evoagentx-agent","user":"alice",
         "messages":[{"role":"user","content":"what skills do you have?"}]}'
  ```

  Or point any OpenAI client at `http://<host>:8000/v1`.

**Capabilities** the agent may use, one per step, up to 8 steps: `list_skills`,
`load_skill`, `search_memory`, `remember`, and every available sub-tool from
`GET /api/tools` (built-in toolkits and this Studio's custom tools).

Tool calls travel as JSON rather than the provider's native protocol, because
`llm.chat()` returns a string for every provider type and only some providers
support tool calls — the loop works whichever model `llm/providers.json`
selects.

**Access.** Unauthenticated by default. Setting `EAX_AGENT_KEY` requires
`Authorization: Bearer <key>`, which OpenAI clients already send. Set it
whenever Studio is reachable beyond this machine: the agent runs tools, and
some of those execute code.

### Chat (conversational workflow building)

Builds and edits the canvas by conversation. The client sends the graph it is
**currently showing** (unsaved edits included) and gets back the graph after
the model's edits; nothing is written to disk, so saving stays explicit.

The model does not use native function calling — `llm.chat()` returns a
string for every provider type and only some providers support tool calls.
Instead it answers with one JSON object, `{"reply", "operations"}`, which the
backend parses, validates and applies itself.

- `POST /api/graphs/{graph_id}/chat`
  ```jsonc
  {"message": "add a node that summarises the findings",
   "history": [{"role": "user"|"assistant", "content": "..."}],
   "graph": { ...canvas graph... }}
  ```
  →
  ```jsonc
  {"reply": "...",              // plain language, in the user's language
   "operations": [...],         // what the model asked for
   "graph": {...},              // canvas to show: unchanged if `errors`
   "applied": true,             // false ⇒ nothing changed
   "errors": [...],             // validation errors (graph left untouched)
   "notes": [...],              // per-operation warnings; the rest still applied
   "tools_created": ["ticker_pe"],
   "job_id": "ab12cd34ef56"}    // only for generate_workflow (see below)
  ```

  Operations fall into three groups:

  - **Canvas edits** — `add_node`, `update_node`, `delete_node`, `rename_node`
    (edges follow), `add_edge`, `delete_edge`, `set_goal`, `set_name`,
    `set_output_dir`, `auto_layout`, `generate_workflow`.
  - **Assets** — `create_tool` / `delete_tool` (the custom-tool path above),
    `create_skill` / `delete_skill` (the skills path above). What was created
    comes back in `tools_created` / `skills_created`.
  - **Reads and actions** — `validate`, `inspect_node`, `list_runs`, `read_run`,
    `list_files`, `read_file`, `write_file`, `delete_file`, `make_dir`,
    `save_graph`, `run_workflow`.

  **Agent loop.** Read operations return their result to the model, which then
  gets another turn to act on it, up to `MAX_STEPS` (6) rounds per message. That
  is what makes debugging possible: `read_run` → diagnose from the failing
  node's real output → `update_node` → `validate`. The response's `steps` says
  how many rounds carried operations, and `operations` is every operation across
  all of them.

  `applied` means the canvas actually changed (compared before/after), so a turn
  of pure reads does not create an undo entry on the client. `saved: true` means
  the model persisted the graph itself (`save_graph`, or implicitly via
  `run_workflow`, which saves so the run matches the canvas).

  `run_workflow` **proposes** a run rather than starting one: the response
  carries `pending_run: {inputs}` and nothing has executed or been saved yet.
  A run spends the user's money and its tools can touch files and external
  services, so the person confirms it — the panel shows the inputs with a
  confirm button and starts the run through the ordinary run path (which saves
  the canvas first). When it settles, the panel sends one automatic follow-up
  turn with the outcome, so the diagnose-and-fix loop still runs unprompted.

  A malformed single operation becomes a `note` and is skipped — one bad edge
  does not discard four good nodes. If the result fails `validate_graph`, the
  model gets one repair round with the validator's errors; if it still fails,
  `graph` comes back unchanged with `errors` set.

- `generate_workflow` runs the framework's `WorkFlowGenerator` (a planner call
  plus one agent call per subtask — minutes, not seconds), so the POST returns
  immediately with a `job_id` and `applied: false`, and the client polls:

  `GET /api/graphs/{graph_id}/chat/jobs/{job_id}` →
  ```jsonc
  {"status": "running"|"done"|"failed",
   "stage": "Designing 6 agents…", "elapsed": 41,
   "graph": {...},   // on done: the generated canvas, already laid out
   "error": "..."}   // on failed
  ```
  Generation REPLACES every node on the canvas. Jobs are in-memory and expire
  an hour after their last update.

## Run mechanics (backend)

- Runs execute in a daemon thread; state kept in a module-level dict and
  persisted to `studio/data/runs/{run_id}.json` on completion.
- Node status is read live off the executing `WorkFlowGraph` nodes
  (`node.status`, values pending/running/completed/failed).
- Node output captured after run from the workflow `Environment`
  execution data (best effort per node output name).
- LLM: `LiteLLM(LiteLLMConfig(model="deepseek/deepseek-v4-flash",
  deepseek_key=os.getenv("DEEPSEEK_API_KEY"), timeout=120))`,
  `load_dotenv()` from repo root.
- Graph construction: `SequentialWorkFlowGraph(goal=..., tasks=[{...without
  x/y...}])` with tasks ordered by topological sort of edges (input order
  breaks ties). Agents via `AgentManager().add_agents_from_workflow(graph,
  llm_config=llm.config)`.
- `use_long_term_memory` is a Studio-level task key (stripped before graph
  construction, like x/y); the runner wires it to the framework's
  `LongTermMemory` per agent (see Memory above).
- Batches persist to `studio/data/batches/{batch_id}.json` on completion;
  each record runs via `runner.start_run(..., background=False)` so it also
  appears under `GET /api/runs/{run_id}`.
- Run from repo root; backend inserts repo root into `sys.path`.
