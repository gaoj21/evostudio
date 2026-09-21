# Generic memory bindings

Memory content, record identity, entity matching, and business time are independent. No field named `company`, `window_start`, `window_end`, or `as_of` is required by the memory subsystem.

## Configuration

New local memories enabled through the inspector use structured records, append writes, and no time filter. Existing configurations retain their storage backend and legacy write semantics until explicitly changed.

```json
{
  "version": 2,
  "kind": "table",
  "write_mode": "append",
  "inputs": [],
  "outputs": ["summary"],
  "match": "nodes.loader.outputs.customer_id",
  "at": "nodes.loader.outputs.as_of",
  "time_filter": true
}
```

`match` and `at` are optional. Remove both for recent-record retrieval across all entities. `at` records business time; `time_filter` separately controls whether reads must precede the current bound time. System write time is always recorded separately. ISO dates/datetimes are accepted; naive values are interpreted as UTC. Offset timestamps are compared by actual instant.

Bindings may reference node inputs, node outputs, or nested JSON fields. Qualified examples: `nodes.loader.outputs.as_of`, `nodes.extract.outputs.document.created_at`. Unqualified references remain supported for old workflows, but qualified references avoid ambiguity when several nodes produce the same field. A qualified missing reference never falls back to a similarly named field from another node.

Metadata bindings are resolved independently of selected content and its character budget. No extra Agent prompt input or “Also record” checkbox is necessary. Explicit additional content fields still use `context`. These are optional: unavailable fields do not invalidate the graph, are skipped during recording, and generate a Memory note in run results. Entity, unique-key and time bindings remain validated.

A node output is available for its post-run write, but not its pre-execution read. Configure read cutoffs using external inputs or completed upstream outputs. Missing configured entity/time values produce a visible Memory error and skip that operation. The workflow continues under its existing optional-memory error policy. A missing cutoff never permits unrestricted retrieval.

## Fields come from the data, not from the pipeline

Match, time and unique-key bindings name fields of whatever the pipeline processes (`customer`, `ticket_id`, `created_at`, `device`). An unqualified binding that no node produces is taken as an **optional workflow input**: the run form asks for it and a batch keeps that column instead of dropping it. Only a qualified reference (`nodes.<name>.…`) to a node field that does not exist is a configuration error.

A missing value degrades memory, never the run:

- entity or business time missing on write: the record is kept without it (append mode) and the run notes that it will not be returned by entity-matched or time-filtered reads; the legacy update-by-entity-and-date mode stores nothing and says so;
- unique key missing on an upsert: refused, because it cannot know which record to update;
- time cutoff missing on a time-filtered read: reading is skipped, with a message saying which field to provide or that the filter can be turned off. It never falls back to reading everything.

## Batch order follows the memory configuration

A record that reads memory only sees what earlier records wrote if they finished first. The batch decides the order from the memory settings (`backend/features/memory/sequencing.py`), not from field names:

- memory matched on a field the records carry: records with the same value run in batch order, different values run in parallel;
- memory not split by a field (recent records, semantic recall, a Mem0 space), matched on a field only a run produces, or matched on different fields by different nodes: the batch runs one record at a time;
- no node reads memory written in this workflow: no ordering.

DataLoader groups and Input trajectories are additional ordering constraints. They never override Memory dependencies. When explicit grouping and Memory dependencies coexist, execution conservatively serializes the records, preserving both constraints. Without Memory dependencies, explicit groups retain their existing parallelism.

## Record identity

- `upsert` with an explicit key can retain an unlinked record when an optional entity value is absent. Only a missing unique key prevents that update.
- `append`: one record per node execution ID. Re-saving that same execution is idempotent. Different executions on the same date remain separate.
- `upsert`, version 2: requires an explicit `key` field binding; updates the record with that key even if its time changes.
- Legacy upsert: retains entity-plus-date identity for compatibility.

Retry idempotency applies when the same run ID is reused. A new run ID represents a new event.

Existing SQLite tables are transactionally migrated to a record-ID primary key on first access. Existing rows and payloads are retained. Browsing, counts, clearing, Chat reads, and exported projects use the same store.

## Inspector

The inspector separates content, retrieval method, write mode, and optional time filtering. Field selectors include upstream fields and explicit node-qualified references. Missing old “Also record” fields remain visible and can be removed. The binding summary shows configured sources, not fabricated sample values.

Switching the backend does not silently delete bindings. The current Mem0 adapter supports semantic shared-space recall; exact entity/key and business-time filters are rejected explicitly. A semantic local store is not advertised as an exact-match implementation.

## Current boundaries

Local memories remain owned by one Agent, but `memory.store_id` decouples their storage and canvas identity from the Agent name. New inspector-created memories receive a UUID; the first rename of a legacy memory pins its original name as the store ID. Qualified field bindings and reader node names follow a rename; store IDs, Chat bindings, saved handles and positions stay unchanged. Shared writable resources remain Mem0 spaces. Project/experiment namespace isolation is not introduced: local memory remains under the workflow ID. Scheduled occurrences retain this same storage and a stable session. Separate manually launched workflows are not serialized with a schedule.

The inspector currently shows binding configuration; an interactive preview of resolved values from historical runs is not included. Memory read/write failures continue to skip the operation and appear in run results; selectable fail-the-workflow behavior is not implemented.

## Implementation map

- `backend/features/memory/bindings.py`: field resolution, validation, runtime context, date parsing, and the workflow inputs memory bindings add.
- `backend/features/memory/sequencing.py`: which batch records must run in order because of memory.
- `backend/features/execution/batch.py`: applies that order (`_group_key`, `_grouped`).
- `backend/features/memory/memory_policy.py`: selection and read/write policy.
- `backend/features/memory/table_store.py`: transactional migration, append/upsert, time-aware reads.
- `backend/features/workflow/runner.py`: authoritative per-node inputs/outputs and execution identities.
- `frontend/src/features/memory/MemorySettings.jsx`: inspector controls.
- `backend/api/export_api.py` and `export_templates.py`: standalone workflow parity.

The LLM provider folder and framework model implementations are unchanged.
