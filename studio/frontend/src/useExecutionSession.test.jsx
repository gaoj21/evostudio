import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { useExecutionSession } from './useExecutionSession.js';

const api = vi.hoisted(() => ({
  abandonRun: vi.fn(),
  cancelBatch: vi.fn(),
  getBatch: vi.fn(),
  getRun: vi.fn(),
  listBatches: vi.fn(),
}));

vi.mock('./api.js', () => ({ api }));

beforeEach(() => {
  vi.clearAllMocks();
  api.listBatches.mockResolvedValue({ batches: [] });
  api.getRun.mockResolvedValue({ run_id: 'run-1', status: 'success', nodes: [], result: 'done' });
  api.getBatch.mockResolvedValue({ batch_id: 'batch-1', status: 'completed', items: [] });
});

function setup() {
  const onError = vi.fn();
  const onClearSelection = vi.fn();
  const hook = renderHook(() => useExecutionSession({
    graphId: 'graph-1',
    onError,
    onClearSelection,
  }));
  return { ...hook, onError, onClearSelection };
}

describe('execution session', () => {
  it('moves a launched run through polling into its result drawer', async () => {
    const { result, onClearSelection } = setup();

    await act(async () => {
      await result.current.launchRun(async () => ({
        run_id: 'run-1', status: 'running', nodes: [], result: null, error: null,
      }));
    });

    expect(result.current.runMode).toBe(true);
    expect(onClearSelection).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(result.current.run?.status).toBe('success'));
    expect(result.current.drawerOpen).toBe(true);
    expect(result.current.drawerTab).toBe('result');
  });

  it('keeps batch activation internally consistent', async () => {
    const { result } = setup();

    act(() => result.current.beginBatch('batch-1'));

    expect(result.current.runMode).toBe(true);
    expect(result.current.run).toBeNull();
    expect(result.current.batch?.batch_id).toBe('batch-1');
    expect(result.current.drawerTab).toBe('items');
    await waitFor(() => expect(result.current.batch?.status).toBe('completed'));
    expect(result.current.drawerOpen).toBe(true);
  });

  it('resets all execution state when another workflow opens', async () => {
    api.getBatch.mockReturnValue(new Promise(() => {}));
    api.listBatches.mockReturnValue(new Promise(() => {}));
    const { result } = setup();
    act(() => result.current.beginBatch('batch-1'));
    act(() => result.current.reset());

    expect(result.current.runMode).toBe(false);
    expect(result.current.run).toBeNull();
    expect(result.current.batch).toBeNull();
    expect(result.current.unattended).toBeNull();
    expect(result.current.drawerOpen).toBe(false);
    expect(result.current.drawerTab).toBe('result');
  });

  it('reports launch failures without entering run mode', async () => {
    const { result, onError } = setup();
    const failure = new Error('cannot start');

    await act(async () => {
      expect(await result.current.launchRun(async () => { throw failure; })).toBeUndefined();
    });

    expect(onError).toHaveBeenCalledWith(failure);
    expect(result.current.runMode).toBe(false);
    expect(result.current.runStarting).toBe(false);
  });
});
