import { useCallback, useRef, useState } from 'react';
import { api } from '../../api.js';

function requestId() {
  if (typeof globalThis.crypto?.randomUUID === 'function') return globalThis.crypto.randomUUID();
  // randomUUID is unavailable on ordinary HTTP origins; getRandomValues is not.
  if (typeof globalThis.crypto?.getRandomValues === 'function') {
    const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
    return Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

export function useChatCancellation(graphId) {
  const current = useRef(null);
  const [stopping, setStopping] = useState(false);
  const [stopError, setStopError] = useState('');
  // `id` re-attaches to a turn already running on the server, so Stop still
  // reaches it after the chat was closed and reopened.
  const begin = useCallback((id = requestId()) => {
    const turn = { id, controller: new AbortController(), stopped: false };
    current.current = turn;
    setStopError(''); setStopping(false);
    return turn;
  }, []);
  const attachJob = useCallback(jobId => {
    const turn = current.current || begin();
    turn.jobId = jobId;
    return turn;
  }, [begin]);
  const isCurrent = useCallback(turn => current.current === turn, []);
  const finish = useCallback(turn => { if (current.current === turn) current.current = null; }, []);
  const stop = useCallback(async () => {
    const turn = current.current;
    if (!turn || stopping) return false;
    turn.stopped = true;
    setStopping(true); setStopError('');
    try {
      if (turn.jobId) await api.stopChatJob(graphId, turn.jobId);
      else await api.stopAssistant(graphId, turn.id);
      turn.controller.abort();
      return true;
    } catch (error) {
      turn.stopped = false;
      setStopError(String(error.body?.detail || error.message));
      return false;
    } finally { setStopping(false); }
  }, [graphId, stopping]);
  return { current, begin, attachJob, isCurrent, finish, stop, stopping, stopError };
}
