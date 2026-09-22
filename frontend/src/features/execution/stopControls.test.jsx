/**
 * The state behind the Stop controls, from the click to a terminal status.
 *
 * Two ways a Stop button goes wrong on screen: it disappears, so nothing says
 * the click was taken; or it sticks on "Stopping…" because the thing it
 * stopped never reports a terminal status. Both are decided here — the hook
 * owns the status the badge and the button read — so both are pinned here.
 */
import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { RUN_SETTLED, BATCH_SETTLED, isSettled } from './runStates.js';
import { useExecutionSession } from './useExecutionSession.js';

const api = vi.hoisted(() => ({
  abandonRun: vi.fn(),
  cancelRun: vi.fn(),
  cancelBatch: vi.fn(),
  getBatch: vi.fn(),
  getRun: vi.fn(),
  listBatches: vi.fn(),
}));

vi.mock('../../api.js', () => ({ api }));

beforeEach(() => {
  vi.clearAllMocks();
  api.listBatches.mockResolvedValue({ batches: [] });
});

function setup() {
  const onError = vi.fn();
  const hook = renderHook(() => useExecutionSession({
    graphId: 'graph-1', onError, onClearSelection: vi.fn(),
  }));
  return { ...hook, onError };
}

async function launch(result, status = 'running') {
  await act(async () => {
    await result.current.launchRun(async () => ({
      run_id: 'run-1', status, nodes: [], result: null, error: null,
    }));
  });
}

describe('stopping a run', () => {
  it('shows the stop as taken until the run settles, and not after', async () => {
    // The engine reports `running` until it has unwound out of the call it
    // was inside, so a poll lands between the click and the verdict. The
    // button has to stay on screen, disabled, across that poll.
    const status = { value: 'running' };
    api.getRun.mockImplementation(async () => (
      { run_id: 'run-1', status: status.value, nodes: [] }));
    api.cancelRun.mockResolvedValue({ cancelled: true });
    const { result } = setup();
    await launch(result);

    await act(async () => { await result.current.stopRun(); });
    expect(result.current.runStopRequested).toBe(true);
    expect(isSettled(RUN_SETTLED, result.current.run.status)).toBe(false);

    // A poll saying "running" again must not offer the stop a second time.
    await act(async () => { await new Promise((done) => { setTimeout(done, 2100); }); });
    expect(result.current.runStopRequested).toBe(true);

    status.value = 'cancelled';
    // The run poll runs every two seconds; give it one.
    await waitFor(() => expect(result.current.run.status).toBe('cancelled'),
      { timeout: 4000 });
    // Settled: nothing is left claiming to be stopping.
    expect(result.current.runStopRequested).toBe(false);
  });

  it('does not claim to be stopping when the server declined', async () => {
    api.getRun.mockResolvedValue({ run_id: 'run-1', status: 'running', nodes: [] });
    api.cancelRun.mockResolvedValue({ cancelled: false, reason: 'run already success' });
    const { result, onError } = setup();
    await launch(result);

    await act(async () => { await result.current.stopRun(); });

    expect(result.current.run.status).toBe('running');
    expect(onError).toHaveBeenCalledWith([expect.stringContaining('run already success')]);
  });
});

describe('stopping a batch', () => {
  it('keeps polling through "cancelling" until the batch settles', async () => {
    api.cancelBatch.mockResolvedValue({
      cancelled: true, not_started: 8, interrupted: 2, finished: 0,
    });
    api.getBatch
      .mockResolvedValueOnce({ batch_id: 'batch-1', status: 'running', items: [] })
      .mockResolvedValue({ batch_id: 'batch-1', status: 'cancelled', items: [] });
    const { result } = setup();
    act(() => result.current.beginBatch('batch-1'));

    await act(async () => { await result.current.cancelBatch(); });
    expect(isSettled(BATCH_SETTLED, 'cancelling')).toBe(false);

    // Were `cancelling` treated as settled, polling would stop here and the
    // badge would read "stopping" for as long as the tab stayed open.
    await waitFor(() => expect(result.current.batch.status).toBe('cancelled'));
    expect(isSettled(BATCH_SETTLED, result.current.batch.status)).toBe(true);
  });

  it('leaves the badge alone when the server refused to stop it', async () => {
    api.getBatch.mockResolvedValue({ batch_id: 'batch-1', status: 'running', items: [] });
    api.cancelBatch.mockResolvedValue({ cancelled: false, reason: 'no such batch' });
    const { result, onError } = setup();
    act(() => result.current.beginBatch('batch-1'));

    await act(async () => { await result.current.cancelBatch(); });

    expect(result.current.batch.status).toBe('running');
    expect(onError).toHaveBeenCalledWith([expect.stringContaining('may still be running')]);
  });

  it('stops the batch that is running, not the one being read', async () => {
    api.getBatch.mockResolvedValue({ batch_id: 'batch-1', status: 'succeeded', items: [] });
    api.listBatches.mockResolvedValue({ batches: [
      { batch_id: 'batch-1', status: 'succeeded' },
      { batch_id: 'batch-other', status: 'running' },
    ] });
    api.cancelBatch.mockResolvedValue({ cancelled: true, not_started: 3, interrupted: 1 });
    const { result } = setup();
    act(() => result.current.beginBatch('batch-1'));

    await waitFor(() => expect(result.current.unattended).toBe('batch-other'));
    await act(async () => { await result.current.cancelBatch(result.current.unattended); });

    expect(api.cancelBatch).toHaveBeenCalledWith('batch-other');
  });
});

describe('the Stop of a batch running out of sight', () => {
  it('says the click landed instead of offering the same Stop again', async () => {
    // It is still listed as live — a cancelling batch is still spending — so
    // the only thing that can say Stop was taken is the button itself.
    api.getBatch.mockResolvedValue({ batch_id: 'shown', status: 'completed', items: [] });
    api.listBatches.mockResolvedValue({ batches: [{ batch_id: 'other', status: 'running' }] });
    api.cancelBatch.mockResolvedValue({ cancelled: true, not_started: 3, interrupted: 0 });
    const { result } = renderHook(() => useExecutionSession({ graphId: 'g' }));

    await waitFor(() => expect(result.current.unattended).toBe('other'));
    expect(result.current.unattendedStopping).toBe(false);
    await act(async () => { await result.current.cancelBatch('other'); });

    api.listBatches.mockResolvedValue({ batches: [{ batch_id: 'other', status: 'cancelling' }] });
    await waitFor(() => expect(result.current.unattendedStopping).toBe(true), { timeout: 8000 });

    api.listBatches.mockResolvedValue({ batches: [{ batch_id: 'other', status: 'cancelled' }] });
    await waitFor(() => expect(result.current.unattended).toBeNull(), { timeout: 8000 });
    expect(result.current.unattendedStopping).toBe(false);
  }, 30000);   // the unattended listing is polled every five seconds
});
