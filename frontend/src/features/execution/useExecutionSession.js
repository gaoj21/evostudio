import { useCallback, useEffect, useMemo, useState } from 'react';

import { api } from '../../api.js';
import { cancelOutcome, unattendedBatch } from './batchControl.js';

import { BATCH_SETTLED, RUN_SETTLED } from './runStates.js';

export { BATCH_SETTLED, RUN_SETTLED };
const isSettled = (settled, status) => settled.includes(status);

/**
 * Owns the lifecycle of the execution shown on the canvas.
 *
 * Editing state stays in App; run polling, batch polling and the result drawer
 * move together here so switching one cannot leave stale pieces of another.
 */
export function useExecutionSession({ graphId, onError, onClearSelection }) {
  const [runStarting, setRunStarting] = useState(false);
  const [runMode, setRunMode] = useState(false);
  const [run, setRun] = useState(null);
  // Sticky until the run settles. The server marks a run `cancelled` only
  // once the engine has unwound out of the call it was inside, so the next
  // poll reports `running` again — and the Stop button went back to offering
  // a stop that had already been accepted.
  const [runStopRequested, setRunStopRequested] = useState(false);
  const [batch, setBatch] = useState(null);
  const [unattended, setUnattended] = useState(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerTab, setDrawerTab] = useState('result');

  const reset = useCallback(() => {
    setRunMode(false);
    setRun(null);
    setRunStopRequested(false);
    setBatch(null);
    setUnattended(null);
    setDrawerOpen(false);
    setDrawerTab('result');
  }, []);

  // Answers plainly: the caller (a dialog, the chat) is the one that has to
  // show a refusal, so it must be able to tell one from a start.
  const launchRun = useCallback(async (start) => {
    setRunStarting(true);
    try {
      const started = await start();
      if (!started?.run_id) {
        return { ok: false, error: new Error('The run did not start.') };
      }
      setRun(started);
      setRunStopRequested(false);
      setBatch(null);
      setRunMode(true);
      setDrawerOpen(false);
      setDrawerTab('result');
      onClearSelection?.();
      return { ok: true, runId: started.run_id };
    } catch (err) {
      return { ok: false, error: err };
    } finally {
      setRunStarting(false);
    }
  }, [onClearSelection]);

  const beginBatch = useCallback((batchId) => {
    setBatch({ batch_id: batchId, status: 'running', node_progress: null, items: [] });
    setRun(null);
    setRunMode(true);
    setDrawerOpen(false);
    setDrawerTab('items');
    onClearSelection?.();
  }, [onClearSelection]);

  const openPastRun = useCallback(async (listedRun) => {
    try {
      const full = (await api.getRun(listedRun.run_id)) || listedRun;
      setBatch(null);
      setRun(full);
      setRunStopRequested(false);
      setRunMode(true);
      setDrawerTab('result');
      setDrawerOpen(true);
      onClearSelection?.();
    } catch (err) {
      onError?.(err);
    }
  }, [onClearSelection, onError]);

  const openPastBatch = useCallback(async (listedBatch) => {
    try {
      const full = await api.getBatch(listedBatch.batch_id);
      setRun(null);
      setBatch(full);
      setRunMode(true);
      setDrawerTab('items');
      setDrawerOpen(true);
      onClearSelection?.();
    } catch (err) {
      onError?.(err);
    }
  }, [onClearSelection, onError]);

  useEffect(() => {
    if (!runMode || !run?.run_id || isSettled(RUN_SETTLED, run.status)) return undefined;
    let stopped = false;
    const tick = async () => {
      try {
        const next = await api.getRun(run.run_id);
        if (stopped) return;
        setRun(next);
        if (isSettled(RUN_SETTLED, next.status)) setDrawerOpen(true);
      } catch (err) {
        if (stopped || err?.status !== 404) return;
        stopped = true;
        setRun((current) => (current?.run_id === run.run_id
          ? {
            ...current,
            status: 'failed',
            error: `Run ${run.run_id} is no longer on the server — it was lost `
              + 'before it finished (a restart, most likely).',
          }
          : current));
        setDrawerOpen(true);
      }
    };
    tick();
    const timer = setInterval(tick, 2000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [runMode, run?.run_id, run?.status]);

  useEffect(() => {
    if (!runMode || !batch?.batch_id || isSettled(BATCH_SETTLED, batch.status)) {
      return undefined;
    }
    let stopped = false;
    const tick = async () => {
      try {
        const next = await api.getBatch(batch.batch_id);
        if (stopped) return;
        setBatch(next);
        if (isSettled(BATCH_SETTLED, next.status)) setDrawerOpen(true);
      } catch (err) {
        if (stopped) return;
        if (err?.status !== 404) {
          setBatch(current => current?.batch_id === batch.batch_id
            ? {...current, polling_error: `Cannot refresh run status: ${err?.body?.detail || err?.message || 'connection failed'}. The displayed progress may be outdated; retrying automatically.`}
            : current);
          setDrawerOpen(true);
          return;
        }
        stopped = true;
        setBatch((current) => (current?.batch_id === batch.batch_id
          ? { ...current, status: 'lost' }
          : current));
        onError?.([`Batch ${batch.batch_id} is no longer on the server — `
          + 'it was lost before it finished.']);
      }
    };
    tick();
    const timer = setInterval(tick, 2000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [runMode, batch?.batch_id, batch?.status, onError]);

  useEffect(() => {
    if (!graphId) return undefined;
    let stopped = false;
    const look = async () => {
      try {
        const listed = await api.listBatches(graphId);
        if (!stopped) {
          setUnattended(unattendedBatch(listed?.batches || listed, batch?.batch_id));
        }
      } catch {
        // This badge is a warning rather than a source of truth. Retry later.
      }
    };
    look();
    const timer = setInterval(look, 5000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [graphId, batch?.batch_id, batch?.status]);

  const cancelBatch = useCallback(async (batchId) => {
    const target = typeof batchId === 'string' ? batchId : batch?.batch_id;
    if (!target) return;
    try {
      const { stopping, message } = cancelOutcome(await api.cancelBatch(target));
      if (stopping) {
        setBatch((current) => (current?.batch_id === target
          ? { ...current, status: 'cancelling' }
          : current));
      }
      if (message) onError?.([message]);
    } catch (err) {
      onError?.(err);
    }
  }, [batch?.batch_id, onError]);

  // Pick a stopped batch up where it left off: the server re-queues what did
  // not finish, and this tab follows it as if it had just been started.
  const resumeBatch = useCallback(async (batchId) => {
    const target = typeof batchId === 'string' ? batchId : batch?.batch_id;
    if (!target) return { ok: false };
    try {
      const outcome = await api.resumeBatch(target);
      const full = await api.getBatch(target);
      setRun(null);
      setBatch({ ...full, status: 'running' });
      setRunMode(true);
      setDrawerTab('items');
      onClearSelection?.();
      return { ok: true, ...outcome };
    } catch (err) {
      onError?.(err);
      return { ok: false, error: err };
    }
  }, [batch?.batch_id, onClearSelection, onError]);

  // Stop the run where it is. The engine cancels the model call it is inside
  // and marks the run `cancelled`; what it had produced stays readable.
  const stopRun = useCallback(async () => {
    if (!run?.run_id) return;
    try {
      const outcome = await api.cancelRun(run.run_id);
      if (!outcome?.cancelled) {
        onError?.([`This run was not stopped: ${outcome?.reason || 'the server declined'}.`]);
        return;
      }
      setRunStopRequested(true);
      setRun((current) => (current?.run_id === run.run_id
        ? { ...current, status: 'cancelling' }
        : current));
    } catch (err) {
      onError?.(err);
    }
  }, [run?.run_id, onError]);
  const abandonRun = stopRun;   // the old name, for callers not yet renamed

  const runByName = useMemo(() => {
    const records = {};
    (run?.nodes || []).forEach((node) => {
      records[node.name] = node;
    });
    return records;
  }, [run]);

  const batchProgress = useMemo(() => {
    const items = batch?.items || [];
    return {
      total: batch?.streaming && !batch?.collection_complete
        ? (batch.input_total ?? null) : (batch?.total ?? items.length),
      // A blocked step (its predecessor did not finish) is settled, and it
      // is not a success: it counts with the failures, so the badge's ✗ says
      // how many records a resume would have to run.
      done: items.filter((item) => ['success', 'failed', 'blocked'].includes(item.status)).length,
      failed: items.filter((item) => item.status === 'failed' || item.status === 'blocked').length,
    };
  }, [batch]);

  return {
    abandonRun,
    stopRun,
    runStopRequested: runStopRequested && !isSettled(RUN_SETTLED, run?.status),
    batch,
    batchProgress,
    beginBatch,
    cancelBatch,
    resumeBatch,
    drawerOpen,
    drawerTab,
    exitRunMode: reset,
    launchRun,
    openPastBatch,
    openPastRun,
    reset,
    run,
    runByName,
    runMode,
    runStarting,
    setDrawerOpen,
    setDrawerTab,
    unattended,
  };
}
