# Scheduled experiments and recovery

Schedule runs the complete workflow on a daily, weekly, or fixed-interval timer. It is separate from Input Watch polling. The Schedule panel exposes weekday, clock time, IANA timezone, inputs, and recovery policy.

## Identity and memory

Each saved schedule has a persistent `experiment_id`, session, original due cursor (`next_fire`), active occurrence (`in_flight`), and last completed due time. A successful workflow occurrence completes one period, not the schedule. The schedule remains enabled and waits for its next planned time.

Occurrences reuse the same workflow Memory stores and session. Pause, process restart, and Resume do not clear memory or create a new experiment. Memory namespace remains workflow-based: manual runs of the same workflow can also access those stores. Removing a schedule deletes its timing configuration, not its Memory data.

## Resume choices

The user can choose a saved automatic recovery policy, or the default **Wait for my recovery choice**. Resume offers:

- **All:** execute missed occurrences serially on the original cadence.
- **Latest:** execute only the most recent due occurrence, then continue normally.
- **Skip:** move to the next future occurrence without executing missed periods.

Example: Monday September 21 succeeds. The application stops Wednesday September 23. Resuming on Monday September 28 does not replay September 21. The September 28 occurrence is pending; the next regular occurrence is October 5. The original experiment/session and Memory remain in use.

If the user resumes several weeks late, All visits each missed Monday; Latest visits only the last due Monday; Skip waits for the upcoming Monday. The cadence is not reset to the resume timestamp.

## Completion and failure

A due occurrence is atomically persisted before dispatch. Its execution ID (Run or Batch) is deterministic from the experiment and original due time. The cursor advances only after the Run succeeds, or every record in the scheduled Batch succeeds. An in-flight occurrence prevents overlapping scheduled runs. A failed or interrupted occurrence requests recovery instead of being silently marked complete.

On process startup, ordinary run reconciliation marks interrupted runs. Schedule then checks the persisted occurrence: a successfully completed run advances the cursor without repeating it, while an incomplete occurrence follows the selected recovery policy. The default asks the user before replaying missed work.

For a Python DataLoader, an occurrence uses the same streaming batch engine as the Run dialog. The configured sample limit and batch size apply, including the last partial batch. Resume keeps successful records, retries unfinished dependent records, and continues reading from the saved offset. A Run completed before its Batch checkpoint is reconciled before retrying. A workflow without a per-record DataLoader uses a single Run; interruption retries that Run from the start. There is no checkpoint resume inside an LLM/tool call. Reusing the execution ID prevents duplicate append records in local structured Memory, but arbitrary external tool side effects cannot be guaranteed exactly once.

Configuration and occurrence state are written to a temporary file, flushed, and atomically replaced. Failed writes are reported instead of silently discarded. The application must be running to execute scheduled work; downtime is handled on recovery.

## Time supplied to the workflow

The optional **Scheduled time input** names a workflow input that receives the original scheduled ISO timestamp. The field is passed into both workflow inputs and the Dataset factory config (overriding reader defaults). Python Dataset code also receives `config["run_context"]` with `scheduled_at`, `experiment_id`, and `session`, even when no optional time-input name was set. Typed factory parameters can receive a named scheduled-time input directly. A DataLoader or tool can use these values to select the right historical period during catch-up. No domain-specific field is assumed and historical data is not reconstructed automatically. A single-record Run reads only its selected Python Dataset item and closes its worker. Shared reference inputs remain materialized because every record explicitly receives their full contents. Dataset constructors must still be lazy; Studio cannot prevent user code from loading a whole file inside its constructor.

## API

- `GET /api/graphs/{id}/schedule`: status, experiment, cursor, active occurrence, recovery requirement.
- `PUT /api/graphs/{id}/schedule`: configure or pause without deleting the cursor/session.
- `POST /api/graphs/{id}/schedule/resume`, body `{"recovery_policy":"all"}` (`latest` or `skip` also accepted).
- `DELETE /api/graphs/{id}/schedule`: remove the schedule.

Implementation: `backend/features/execution/scheduler.py`; UI: `frontend/src/features/execution/SchedulePanel.jsx`.

## Saved configuration and durability

A Schedule pins a deep copy of the workflow when created. Later canvas edits do not alter that experiment, including retries and future occurrences. Remove and recreate the Schedule to adopt canvas changes; this keeps existing workflow Memory. Legacy schedules pin the available workflow on their first execution after upgrade. An occurrence also persists its own input values and session before dispatch.

Batch Resume uses its original saved workflow and DataLoader configuration. Legacy batches without an execution snapshot retain the older fallback to the current workflow; an original version cannot be reconstructed retrospectively. Uploaded data resources and user-owned external files are not copied into the snapshot. External tool definitions, installed packages and mutable files must remain compatible with the saved workflow.

Run and Batch state use complete temporary JSON files, flush/fsync and atomic replacement. A failed write preserves the prior complete file. Initial persistence failure prevents dispatch. Schedule does not advance on a reported persistence error. Per-record execution IDs and the read cursor are checkpointed before tool execution, so a completed Run can be recovered even if the following Batch checkpoint was interrupted. External side effects interrupted before Run completion still cannot be guaranteed exactly once.
