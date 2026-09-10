# Workflow Chat controls

Workflow Chat operates on the current canvas, including unsaved edits. It can
modify task prompts, fields, source settings, tools, Skills, Memory policies,
harness settings, enablement, connections, layout and workflow configuration.

## Execution

`plan_workflow` compiles the same plan used by Run, reports input errors and
supports `start_at`. `run_workflow` returns a run card with that plan, editable
typed inputs, optional source record and session. Clicking Run it saves the
current design, submits the plan ID and starts the existing workflow runner.
A stale plan is rejected by the API. Missing inputs can be corrected in the
card without starting a failed run. A proposed run is not reported as started.

Chat polls its latest started run, offers Stop while it runs, and requests one
result-summary turn on completion. The handled marker persists with history,
so old runs are not repeatedly summarized. Saving under a new workflow ID
moves the conversation and its run tracking to the new ID.

`list_runs`, `read_run`, `cancel_run`, `list_batches`, `read_batch` and
`cancel_batch` inspect or stop existing execution. Explicit run and batch IDs
must belong to the current workflow. Retrying uses a new run proposal with
reviewable inputs; diagnostics do not automatically launch repeated runs.

## Other controls

- `configure_workflow`: goal, preprocessing and output directory.
- `list_memories` / `read_memory`: inspect workflow memory, including paginated tables.
- `read_schedule` / `set_schedule` / `clear_schedule`: schedule management.
  Setting a schedule validates inputs and saves the current design before
  enabling it. The model is instructed to do this only on explicit requests.
- `read_watch` / `start_watch` / `stop_watch`: configured source watchers.
- `open_panel`: run (including batch input setup), history, schedule,
  optimization, review and workspace controls. Batch creation and file uploads
  use the existing run panel.
- Existing file, tool, Skill, save and generation operations remain available.

These are operation capabilities, not a promise that arbitrary model-selected
parameters are valid. Backend validation remains authoritative; rejected
operations are reported back to the model and shown in the conversation.
