/**
 * One reading of run and batch states, used everywhere they are shown.
 *
 * The canvas badge, the drawer and the history each had their own idea of
 * which states were "done" and which colour they got, and a batch with
 * failed records came out green in one of them. This is the only mapping.
 */

// A run is settled when nothing more will happen to it on the server.
export const RUN_SETTLED = ['success', 'failed', 'cancelled', 'abandoned', 'lost', 'interrupted'];
// A batch is settled likewise; `cancelling` is not — its in-flight records
// are still running and still spending.
export const BATCH_SETTLED = [
  'succeeded', 'completed_with_errors', 'failed', 'cancelled', 'lost', 'interrupted',
  'completed',   // written by earlier versions; read as "ended, look at the counts"
];

export const isSettled = (settled, status) => settled.includes(status);

const RUN_STATES = {
  running: { label: 'running', tone: 'running' },
  success: { label: 'succeeded', tone: 'success' },
  failed: { label: 'failed', tone: 'failed' },
  cancelling: { label: 'stopping', tone: 'running' },
  cancelled: { label: 'stopped', tone: 'stopped',
    note: 'Stopped where it was; the model call in flight was abandoned.' },
  abandoned: { label: 'left running', tone: 'stopped',
    note: 'You stopped waiting; the server finished it on its own.' },
  lost: { label: 'lost', tone: 'stopped',
    note: 'The server no longer knows this run — it was lost before it finished.' },
  interrupted: { label: 'interrupted', tone: 'stopped',
    note: 'The server restarted while this run was in flight; it cannot be resumed.' },
};

const BATCH_STATES = {
  running: { label: 'running', tone: 'running' },
  cancelling: { label: 'stopping', tone: 'running' },
  succeeded: { label: 'succeeded', tone: 'success' },
  completed_with_errors: { label: 'completed with errors', tone: 'partial' },
  failed: { label: 'failed', tone: 'failed' },
  cancelled: { label: 'stopped', tone: 'stopped' },
  lost: { label: 'lost', tone: 'stopped' },
  interrupted: { label: 'interrupted', tone: 'stopped' },
};

export function describeRun(status) {
  return RUN_STATES[status] || { label: status || 'unknown', tone: 'running' };
}

/**
 * A batch's state, read with its counts: a legacy `completed` is green only
 * if nothing in it failed.
 */
export function describeBatch(status, counts = {}) {
  if (status === 'completed') {
    return counts.failed
      ? BATCH_STATES.completed_with_errors
      : { label: 'completed', tone: 'success' };
  }
  return BATCH_STATES[status] || { label: status || 'unknown', tone: 'running' };
}

// Class name for a status pill; `partial` gets its own colour.
export const toneClass = (tone) => ({
  success: 'status-completed', failed: 'status-failed', stopped: 'status-stopped',
  partial: 'status-partial', running: 'status-running',
}[tone] || 'status-running');
