import { describe, expect, it, vi } from 'vitest';

import { singleFlight } from './singleFlight.js';

describe('singleFlight', () => {
  it('skips a call while the previous one is still running', async () => {
    let release;
    const task = vi.fn(() => new Promise((resolve) => { release = resolve; }));
    const poll = singleFlight(task);

    const first = poll();
    poll();
    poll();
    expect(task).toHaveBeenCalledTimes(1);

    release('done');
    await expect(first).resolves.toBe('done');
    poll();
    expect(task).toHaveBeenCalledTimes(2);
  });

  it('runs again after a failure', async () => {
    const task = vi.fn().mockRejectedValueOnce(new Error('down')).mockResolvedValue('ok');
    const poll = singleFlight(task);
    await expect(poll()).rejects.toThrow('down');
    await expect(poll()).resolves.toBe('ok');
  });
});
