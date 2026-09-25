import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';

vi.mock('../../api.js', () => ({ api: { getUsage: vi.fn() } }));
import { api } from '../../api.js';
import UsageTab from './UsageTab.jsx';

const used = (inp, out, extra = {}) => ({ reported: true, input_tokens: inp, output_tokens: out,
  total_tokens: inp + out, reported_calls: 1, cost: { total_cost: 0.0012, input_price_per_1m: 2.5, output_price_per_1m: 15 }, ...extra });

beforeEach(() => api.getUsage.mockReset());

it('shows the total, cached and reasoning tokens, and the cost for a run', async () => {
  api.getUsage.mockResolvedValue({ kind: 'run', running: false,
    total: used(300, 100, { cache_read_tokens: 200, reasoning_tokens: 40 }),
    by_node: [{ node: 'a', usage: used(100, 50), share: 0.375 }, { node: 'src', usage: { reported: false }, share: null }],
    by_record: null });

  render(<UsageTab runId="r1" />);

  const summary = await screen.findByTestId('usage-summary');
  expect(summary.textContent).toContain('400');
  expect(summary.textContent).toContain('cached 200');
  expect(summary.textContent).toContain('reasoning 40');
  expect(summary.textContent).toContain('$0.00120');
  expect(screen.getByTestId('usage-by-node').textContent).toContain('not reported');
  expect(screen.queryByTestId('usage-by-record')).toBeNull();
  expect(api.getUsage).toHaveBeenCalledWith({ runId: 'r1' });
});

it('never shows zero when nothing was reported', async () => {
  api.getUsage.mockResolvedValue({ kind: 'run', running: false, total: { reported: false }, by_node: [], by_record: null });

  render(<UsageTab runId="r1" />);

  expect(await screen.findByTestId('usage-none')).toHaveTextContent('No provider usage was reported');
});

it('lists a batch by record and can put the most expensive first', async () => {
  api.getUsage.mockResolvedValue({ kind: 'batch', running: false, total: used(30, 10),
    by_node: [], by_record: [
      { index: 0, status: 'success', usage: used(5, 1), share: 0.15 },
      { index: 1, status: 'success', usage: used(20, 5), share: 0.625 },
      { index: 2, status: 'pending', usage: { reported: false }, share: null }] });

  render(<UsageTab batchId="b1" />);

  const table = await screen.findByTestId('usage-by-record');
  const firstLabel = () => table.querySelector('tbody tr td').textContent;
  expect(firstLabel()).toContain('#1');
  fireEvent.click(screen.getByLabelText(/most tokens first/));
  await waitFor(() => expect(firstLabel()).toContain('#2'));
  expect(api.getUsage).toHaveBeenCalledWith({ batchId: 'b1' });
});

it('keeps asking while the batch runs and stops once it has settled', async () => {
  vi.useFakeTimers();
  api.getUsage
    .mockResolvedValueOnce({ kind: 'batch', running: true, total: used(10, 1), by_node: [], by_record: [] })
    .mockResolvedValueOnce({ kind: 'batch', running: false, total: used(20, 2), by_node: [], by_record: [] });

  render(<UsageTab batchId="b1" />);
  await vi.advanceTimersByTimeAsync(0);
  await vi.advanceTimersByTimeAsync(2100);
  await vi.advanceTimersByTimeAsync(5000);

  expect(api.getUsage).toHaveBeenCalledTimes(2);
  vi.useRealTimers();
});

it('fetches again whenever the run it shows moves on', async () => {
  api.getUsage.mockResolvedValue({ kind: 'batch', running: false, total: used(10, 1), by_node: [], by_record: [] });
  const { rerender } = render(<UsageTab batchId="b1" live="running" version="running|10|1" />);
  await screen.findByTestId('usage-summary');

  rerender(<UsageTab batchId="b1" live="running" version="running|40|4" />);

  await waitFor(() => expect(api.getUsage).toHaveBeenCalledTimes(2));
});

it('keeps trying after a failed refresh and says when it last updated', async () => {
  vi.useFakeTimers();
  api.getUsage
    .mockResolvedValueOnce({ kind: 'batch', running: true, total: used(10, 1), by_node: [], by_record: [] })
    .mockRejectedValueOnce(new Error('network'))
    .mockResolvedValueOnce({ kind: 'batch', running: false, total: used(30, 3), by_node: [], by_record: [] });

  render(<UsageTab batchId="b1" />);
  await vi.advanceTimersByTimeAsync(0);
  await vi.advanceTimersByTimeAsync(2100);
  expect(screen.getByTestId('usage-updated').textContent).toContain('last refresh failed');
  await vi.advanceTimersByTimeAsync(2100);

  expect(api.getUsage).toHaveBeenCalledTimes(3);
  expect(screen.getByTestId('usage-summary').textContent).toContain('33');
  vi.useRealTimers();
});

it('says how many calls came back without usage, instead of showing nothing', async () => {
  api.getUsage.mockResolvedValue({ kind: 'batch', running: true,
    total: { reported: false, unreported_calls: 12 },
    by_node: [{ node: 'a', usage: { reported: false, unreported_calls: 12 }, share: null }], by_record: [] });

  render(<UsageTab batchId="b1" />);

  expect(await screen.findByTestId('usage-unreported')).toHaveTextContent('12 model calls came back without usage');
  expect(screen.queryByTestId('usage-none')).toBeNull();
  expect(screen.getByTestId('usage-by-node').textContent).toContain('12 calls without usage');
});
