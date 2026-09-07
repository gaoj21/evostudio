import { useCallback, useEffect, useMemo, useState } from 'react';

import { api } from './api.js';
import { cancelOutcome, unattendedBatch } from './batchControl.js';

export const RUN_SETTLED = ['success', 'failed', 'abandoned', 'lost'];
export const BATCH_SETTLED = ['completed', 'cancelled', 'lost', 'interrupted'];
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
  const [batch, setBatch] = useState(null);
  const [unattended, setUnattended] = useState(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerTab, setDrawerTab] = useState('result');

  const reset = useCallback(() => {
    setRunMode(false);
    setRun(null);
    setBatch(null);
    setUnattended(null);
    setDrawerOpen(false);
    setDrawerTab('result');
  }, []);

  const launchRun = useCallback(async (start) => {
    setRunStarting(true);
    try {
      const started = await start();
      if (!started?.run_id) return undefined;
      setRun(started);
      setBatch(null);
      setRunMode(true);
      setDrawerOpen(false);
      setDrawerTab('result');
      onClearSelection?.();
      return started.run_id;
    } catch (err) {
      onError?.(err);
      return undefined;
    } finally {
      setRunStarting(false);
    }
  }, [onClearSelection, onError]);

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
        if (stopped || err?.status !== 404) return;
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

  const abandonRun = useCallback(async () => {
    if (!run?.run_id) return;
    try {
      await api.abandonRun(run.run_id);
      setRun((current) => (current?.run_id === run.run_id
        ? { ...current, status: 'abandoned' }
        : current));
      onError?.(['Given up on this run. It cannot be interrupted, so it finishes '
        + 'in the background and still spends whatever it has left.']);
    } catch (err) {
      onError?.(err);
    }
  }, [run?.run_id, onError]);

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
      total: batch?.total ?? items.length,
      done: items.filter((item) => item.status === 'success' || item.status === 'failed').length,
      failed: items.filter((item) => item.status === 'failed').length,
    };
  }, [batch]);

  return {
    abandonRun,
    batch,
    batchProgress,
    beginBatch,
    cancelBatch,
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
