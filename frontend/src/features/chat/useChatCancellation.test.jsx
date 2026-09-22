/**
 * The two ways a chat Stop button goes wrong: it becomes clickable again while
 * the turn it stopped is still being wound down, or it stays a disabled
 * "Stopping…" after a Stop that never reached the server. The hook owns that
 * state, so both are pinned here.
 */
import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { useChatCancellation } from './useChatCancellation.js';

const api = vi.hoisted(() => ({ stopAssistant: vi.fn(), stopChatJob: vi.fn() }));
vi.mock('../../api.js', () => ({ api }));

beforeEach(() => {
  vi.clearAllMocks();
  api.stopAssistant.mockResolvedValue({ status: 'cancelled' });
});

describe('stopping a chat turn', () => {
  it('stays taken until the turn is released', async () => {
    const { result } = renderHook(() => useChatCancellation('g'));
    let turn;
    act(() => { turn = result.current.begin('req-1'); });

    await act(async () => { expect(await result.current.stop()).toBe(true); });
    expect(api.stopAssistant).toHaveBeenCalledWith('g', 'req-1');
    // The poller has not noticed yet; the button must not invite a second click.
    expect(result.current.stopping).toBe(true);

    act(() => { result.current.finish(turn); });
    expect(result.current.stopping).toBe(false);
    expect(result.current.current.current).toBe(null);
  });

  it('is clickable again when the Stop request itself failed', async () => {
    api.stopAssistant.mockRejectedValue({ body: { detail: 'Unknown request' } });
    const { result } = renderHook(() => useChatCancellation('g'));
    let turn;
    act(() => { turn = result.current.begin('req-2'); });

    await act(async () => { expect(await result.current.stop()).toBe(false); });
    expect(result.current.stopping).toBe(false);
    expect(result.current.stopError).toBe('Unknown request');
    expect(turn.stopped).toBe(false);   // the turn is still going
  });

  it('does not send a second Stop for the same turn', async () => {
    const { result } = renderHook(() => useChatCancellation('g'));
    act(() => { result.current.begin('req-3'); });
    await act(async () => { await result.current.stop(); });
    await act(async () => { expect(await result.current.stop()).toBe(false); });
    expect(api.stopAssistant).toHaveBeenCalledTimes(1);
  });

  it('a new turn starts with Stop untaken', async () => {
    const { result } = renderHook(() => useChatCancellation('g'));
    act(() => { result.current.begin('req-4'); });
    await act(async () => { await result.current.stop(); });
    act(() => { result.current.begin('req-5'); });
    expect(result.current.stopping).toBe(false);
    expect(result.current.stopError).toBe('');
  });
});
