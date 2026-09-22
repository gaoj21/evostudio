import { api } from '../../api.js';

// An assistant turn runs on the server as its own job. The chat that started
// it may be unmounted (another panel, another workflow, a closed drawer); the
// turn keeps going, and the chat re-attaches from this record when it is back.
const pendingKey = storeKey => `${storeKey}:pending-turn`;
export const TURN_POLL_MS = 1000;

export function loadPendingTurn(storeKey) {
  try { return JSON.parse(localStorage.getItem(pendingKey(storeKey)) || 'null'); } catch { return null; }
}
export function savePendingTurn(storeKey, pending) {
  try { localStorage.setItem(pendingKey(storeKey), JSON.stringify(pending)); } catch { /* Re-attach falls back to the server's list. */ }
}
export function clearPendingTurn(storeKey, turnId) {
  try { if (!turnId || loadPendingTurn(storeKey)?.turnId === turnId) localStorage.removeItem(pendingKey(storeKey)); } catch { /* Nothing stored. */ }
}

// Turn ids this browser stopped. A stopped turn settles on the server at once,
// so its list of running turns no longer offers it; this is what keeps a
// remount that races that — or one in another tab — from picking it up again.
const stoppedKey = storeKey => `${storeKey}:stopped-turns`;
const STOPPED_KEPT = 20;

export function rememberStopped(storeKey, turnId) {
  if (!turnId) return;
  try {
    const kept = [turnId, ...loadStopped(storeKey).filter(id => id !== turnId)].slice(0, STOPPED_KEPT);
    localStorage.setItem(stoppedKey(storeKey), JSON.stringify(kept));
  } catch { /* The server's own list is the primary guard. */ }
}
function loadStopped(storeKey) {
  try {
    const kept = JSON.parse(localStorage.getItem(stoppedKey(storeKey)) || '[]');
    return Array.isArray(kept) ? kept : [];
  } catch { return []; }
}
export function wasStopped(storeKey, turnId) {
  return !!turnId && loadStopped(storeKey).includes(turnId);
}

const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

// Poll until the turn settles. Resolves null once `keepGoing()` is false: the
// caller went away, the turn did not, and a later mount picks it up again.
// Transient network errors are retried; only a server that no longer knows
// the turn (e.g. after a restart) ends it as lost.
export async function followTurn(graphId, turnId, { keepGoing, onProgress, pollMs = TURN_POLL_MS }) {
  for (;;) {
    if (!keepGoing()) return null;
    let turn = null;
    try { turn = await api.assistantTurn(graphId, turnId); } catch (error) {
      if (error.status === 404) return { turn_id: turnId, status: 'lost', error: 'The server no longer has this request (it may have restarted). Ask again to continue.' };
      if (keepGoing()) onProgress?.({ turn_id: turnId, status: 'running', warning: `Reconnecting… ${error.message}` });
    }
    if (!keepGoing()) return null;
    if (turn && turn.status !== 'running') return turn;
    if (turn) onProgress?.(turn);
    await wait(pollMs);
  }
}

// What a settled turn means for the chat: its reply, or an error to show.
export function turnOutcome(turn) {
  if (turn.status === 'done') return { result: turn.result };
  if (turn.status === 'cancelled') return { stopped: true };
  const detail = turn.error;
  return { error: typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : 'The request failed.' };
}
