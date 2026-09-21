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

A due occurrence is atomically persisted before dispatch. Its run ID is deterministic from the experiment and original due time. The cursor advances only after the run reports success. An in-flight occurrence prevents overlapping scheduled runs. A failed or interrupted occurrence requests recovery instead of being silently marked complete.

On process startup, ordinary run reconciliation marks interrupted runs. Schedule then checks the persisted occurrence: a successfully completed run advances the cursor without repeating it, while an incomplete occurrence follows the selected recovery policy. The default asks the user before replaying missed work.

Retrying an interrupted occurrence restarts that workflow occurrence; it is not a checkpoint resume inside an LLM/tool call. Reusing the execution ID prevents duplicate append records in local structured Memory, but arbitrary external tool side effects cannot be guaranteed exactly once.

Configuration and occurrence state are written to a temporary file, flushed, and atomically replaced. Failed writes are reported instead of silently discarded. The application must be running to execute scheduled work; downtime is handled on recovery.

## Time supplied to the workflow

The optional **Scheduled time input** names a workflow input that receives the original scheduled ISO timestamp. A DataLoader or tool can use this value to select the right historical period during catch-up. No domain-specific field is assumed and historical data is not reconstructed automatically. Inputs and workflow execution semantics otherwise match an ordinary Run.

## API

- `GET /api/graphs/{id}/schedule`: status, experiment, cursor, active occurrence, recovery requirement.
- `PUT /api/graphs/{id}/schedule`: configure or pause without deleting the cursor/session.
- `POST /api/graphs/{id}/schedule/resume`, body `{"recovery_policy":"all"}` (`latest` or `skip` also accepted).
- `DELETE /api/graphs/{id}/schedule`: remove the schedule.

Implementation: `backend/features/execution/scheduler.py`; UI: `frontend/src/features/execution/SchedulePanel.jsx`.
