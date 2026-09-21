import { afterEach, expect, it, vi } from 'vitest';
import { api } from './api.js';

afterEach(() => vi.unstubAllGlobals());

const stubFetch = () => {
  const fetch = vi.fn(async () => ({ ok: true, json: async () => ({ stopping: true }) }));
  vi.stubGlobal('fetch', fetch);
  return fetch;
};

it('asks any Input type for its details with the given params', async () => {
  const fetch = stubFetch();
  await api.sourceInfo('my type', { version: 'v1', empty: '', none: undefined, n: 0 });
  expect(fetch.mock.calls[0][0]).toBe('/api/sources/my%20type/info?version=v1&n=0');
  await api.sourceInfo('plain');
  expect(fetch.mock.calls[1][0]).toBe('/api/sources/plain/info');
});

it('stops an evolve task and has no task-specific source calls left', async () => {
  const fetch = stubFetch();
  expect(await api.stopEvolve('t/1')).toEqual({ stopping: true });
  expect(fetch).toHaveBeenCalledWith('/api/evolve/t%2F1/stop', expect.objectContaining({ method: 'POST' }));
  for (const gone of ['creditRiskSource', 'runBatchSource', 'previewBatchSource', 'startEvolveSource']) expect(api[gone]).toBeUndefined();
});
