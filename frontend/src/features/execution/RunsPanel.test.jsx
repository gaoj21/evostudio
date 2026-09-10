import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import RunsPanel, { describeBatch, describeSource } from './RunsPanel.jsx';

vi.mock('../../api.js', () => ({
  api: { listRuns: vi.fn(), listBatches: vi.fn(), compareBatches: vi.fn() },
}));

const { api } = await import('../../api.js');

const RUN = {
  run_id: 'r1', status: 'success', inputs: { city: 'Lima' },
  created_at: new Date().toISOString(),
};
const BATCH = {
  batch_id: 'b1', status: 'completed', total: 12,
  counts: { success: 10, failed: 2 }, metric: 'exact_match',
  summary: { mean: 0.83, scored: 10 },
  source: { type: 'upload', filename: 'cities.jsonl' },
  created_at: new Date().toISOString(),
};

function setup(props = {}) {
  const onOpenRun = vi.fn();
  const onOpenBatch = vi.fn();
  render(
    <RunsPanel
      open
      graphId="g1"
      onClose={vi.fn()}
      onOpenRun={onOpenRun}
      onOpenBatch={onOpenBatch}
      {...props}
    />
  );
  return { onOpenRun, onOpenBatch, user: userEvent.setup() };
}

const OLDER = {
  ...BATCH, batch_id: 'b0', summary: { mean: 0.62, scored: 12 },
  created_at: '2026-09-01T00:00:00Z',
};

beforeEach(() => {
  api.listRuns.mockResolvedValue([RUN]);
  api.listBatches.mockResolvedValue([BATCH]);
  api.compareBatches.mockResolvedValue({
    baseline: { batch_id: 'b0' }, candidate: { batch_id: 'b1', metric: 'exact_match' },
    matched: 12, mean_before: 0.62, mean_after: 0.83, delta: 0.21,
    improved: 3, regressed: 1, unchanged: 8, unscored: 0, notes: [],
    changes: [{ label: 'Oslo', delta: -1, score_before: 1, score_after: 0,
                status_before: 'success', status_after: 'success' }],
  });
});

describe('describeBatch', () => {
  it('says what the batch covered and how it ended', () => {
    expect(describeBatch({ total: 12, counts: { success: 10, failed: 2 } }))
      .toBe('12 records · 10 ✓ · 2 ✗');
  });

  it('names the records a cancellation never reached', () => {
    expect(describeBatch({ total: 20, counts: { success: 2, cancelled: 18 } }))
      .toBe('20 records · 2 ✓ · 18 stopped');
  });

  it('keeps the count singular for one record', () => {
    expect(describeBatch({ total: 1, counts: { success: 1 } })).toBe('1 record · 1 ✓');
  });

  it('survives a batch with nothing recorded', () => {
    expect(describeBatch({})).toBe('0 records');
  });
});

describe('describeSource', () => {
  it('names the file the user uploaded', () => {
    expect(describeSource({ type: 'upload', filename: 'cities.jsonl' }))
      .toBe('cities.jsonl');
  });

  it('names the canvas node the records came from', () => {
    expect(describeSource({ type: 'canvas', node: 'feed' })).toBe('canvas · feed');
  });

  it('includes the split for a dataset source', () => {
    expect(describeSource({ type: 'credit_risk', split: 'test' }))
      .toBe('credit_risk · test');
    expect(describeSource({ type: 'credit_risk' })).toBe('credit_risk');
  });

  it('says nothing rather than "undefined" when there is no source', () => {
    expect(describeSource(null)).toBe('');
  });
});

describe('RunsPanel', () => {
  it('opens on runs and counts both kinds', async () => {
    setup();
    expect(await screen.findByRole('button', { name: 'Runs (1)' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Batches (1)' })).toBeInTheDocument();
    expect(screen.getByText('city=Lima')).toBeInTheDocument();
  });

  it('shows a past batch with its score, and hands it back on click', async () => {
    const { onOpenBatch, user } = setup();
    await user.click(await screen.findByRole('button', { name: 'Batches (1)' }));

    expect(screen.getByText('12 records · 10 ✓ · 2 ✗')).toBeInTheDocument();
    expect(screen.getByText('cities.jsonl')).toBeInTheDocument();
    expect(screen.getByText('0.83')).toBeInTheDocument();
    expect(screen.getByText('exact_match')).toBeInTheDocument();

    await user.click(screen.getByTitle('Open this batch on the canvas'));
    expect(onOpenBatch).toHaveBeenCalledWith(BATCH);
  });

  it('does not mix the two lists', async () => {
    const { user } = setup();
    await user.click(await screen.findByRole('button', { name: 'Batches (1)' }));
    expect(screen.queryByText('city=Lima')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Runs (1)' }));
    expect(screen.queryByText('cities.jsonl')).not.toBeInTheDocument();
  });

  it('marks a cancelled batch as neither passed nor failed', async () => {
    api.listBatches.mockResolvedValue([
      { ...BATCH, status: 'cancelled', counts: { success: 2, cancelled: 10 }, summary: null },
    ]);
    const { user } = setup();
    await user.click(await screen.findByRole('button', { name: 'Batches (1)' }));

    const pill = screen.getByText('stopped');
    expect(pill).toHaveClass('status-stopped');
    expect(pill).not.toHaveClass('status-failed');
  });

  it('says so plainly when a workflow has run but never in batch', async () => {
    api.listBatches.mockResolvedValue([]);
    const { user } = setup();
    await user.click(await screen.findByRole('button', { name: 'Batches (0)' }));
    expect(screen.getByText('This workflow has no batch runs yet.')).toBeInTheDocument();
  });

  it('reports a failed lookup instead of loading forever', async () => {
    api.listBatches.mockRejectedValue({ body: { detail: 'batches unreadable' } });
    setup();
    expect(await screen.findByText('batches unreadable')).toBeInTheDocument();
    expect(screen.queryByText('Loading…')).not.toBeInTheDocument();
  });

  it('reloads when a different workflow is opened', async () => {
    const { rerender } = render(
      <RunsPanel open graphId="g1" onClose={vi.fn()}
                 onOpenRun={vi.fn()} onOpenBatch={vi.fn()} />
    );
    await screen.findByRole('button', { name: 'Runs (1)' });
    expect(api.listBatches).toHaveBeenCalledWith('g1');

    api.listRuns.mockResolvedValue([]);
    api.listBatches.mockResolvedValue([]);
    rerender(
      <RunsPanel open graphId="g2" onClose={vi.fn()}
                 onOpenRun={vi.fn()} onOpenBatch={vi.fn()} />
    );

    // Otherwise the previous workflow's history sits there looking like this
    // one's.
    expect(await screen.findByRole('button', { name: 'Runs (0)' })).toBeInTheDocument();
    expect(api.listBatches).toHaveBeenLastCalledWith('g2');
  });

  it('fetches nothing while closed', () => {
    render(<RunsPanel open={false} graphId="g1" onClose={vi.fn()}
                      onOpenRun={vi.fn()} onOpenBatch={vi.fn()} />);
    expect(api.listRuns).not.toHaveBeenCalled();
  });

  it('shows a batch still running, which is how a reload reattaches to it', async () => {
    api.listBatches.mockResolvedValue([
      { ...BATCH, status: 'running', counts: { success: 3, pending: 9 }, summary: null },
    ]);
    const { onOpenBatch, user } = setup();
    await user.click(await screen.findByRole('button', { name: 'Batches (1)' }));

    const row = screen.getByTitle('Open this batch on the canvas');
    expect(within(row).getByText('running')).toHaveClass('status-running');
    await user.click(row);
    expect(onOpenBatch).toHaveBeenCalled();
  });

  it('compares two batches, oldest as the baseline', async () => {
    api.listBatches.mockResolvedValue([BATCH, OLDER]);   // newest first
    const { user } = setup();
    await user.click(await screen.findByRole('button', { name: 'Batches (2)' }));

    await user.click(screen.getByRole('checkbox', { name: 'Compare b1' }));
    expect(screen.getByText('Pick one more to compare against.')).toBeInTheDocument();
    await user.click(screen.getByRole('checkbox', { name: 'Compare b0' }));
    await user.click(screen.getByRole('button', { name: 'Compare' }));

    // The older batch is the baseline whichever order they were ticked in;
    // reversed, every improvement would read as a regression.
    await waitFor(() => expect(api.compareBatches).toHaveBeenCalledWith('b1', 'b0'));
    expect(await screen.findByText('+0.21')).toBeInTheDocument();
    expect(screen.getByText('Oslo')).toBeInTheDocument();
  });

  it('takes exactly two batches, never one or three', async () => {
    const third = { ...OLDER, batch_id: 'b2', created_at: '2026-08-01T00:00:00Z' };
    api.listBatches.mockResolvedValue([BATCH, OLDER, third]);
    const { user } = setup();
    await user.click(await screen.findByRole('button', { name: 'Batches (3)' }));

    // Nothing to compare yet, so no offer to.
    expect(screen.queryByRole('button', { name: 'Compare' })).not.toBeInTheDocument();

    await user.click(screen.getByRole('checkbox', { name: 'Compare b1' }));
    expect(screen.getByRole('button', { name: 'Compare' })).toBeDisabled();
    await user.click(screen.getByRole('checkbox', { name: 'Compare b0' }));
    expect(screen.getByRole('button', { name: 'Compare' })).toBeEnabled();

    // A third is refused rather than silently displacing one of the two.
    const third_box = screen.getByRole('checkbox', { name: 'Compare b2' });
    expect(third_box).toBeDisabled();
    await user.click(screen.getByRole('checkbox', { name: 'Compare b0' }));
    expect(third_box).toBeEnabled();
  });

  it('returns to the list from a comparison', async () => {
    api.listBatches.mockResolvedValue([BATCH, OLDER]);
    const { user } = setup();
    await user.click(await screen.findByRole('button', { name: 'Batches (2)' }));
    await user.click(screen.getByRole('checkbox', { name: 'Compare b1' }));
    await user.click(screen.getByRole('checkbox', { name: 'Compare b0' }));
    await user.click(screen.getByRole('button', { name: 'Compare' }));
    await screen.findByText('+0.21');

    await user.click(screen.getByRole('button', { name: '← Back to batches' }));
    expect(screen.getAllByTitle('Open this batch on the canvas')).toHaveLength(2);
    expect(screen.queryByText('+0.21')).not.toBeInTheDocument();
  });

  it('reports a comparison that failed instead of showing a blank panel', async () => {
    api.listBatches.mockResolvedValue([BATCH, OLDER]);
    api.compareBatches.mockRejectedValue({ body: { detail: 'batch b0 not found' } });
    const { user } = setup();
    await user.click(await screen.findByRole('button', { name: 'Batches (2)' }));
    await user.click(screen.getByRole('checkbox', { name: 'Compare b1' }));
    await user.click(screen.getByRole('checkbox', { name: 'Compare b0' }));
    await user.click(screen.getByRole('button', { name: 'Compare' }));

    expect(await screen.findByText('batch b0 not found')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Compare' })).toBeEnabled();
  });
});


describe('a stopped batch can be picked up from the list', () => {
  it('shows how many records are left and hands the batch to the resume handler', async () => {
    api.listRuns.mockResolvedValue([]);
    api.listBatches.mockResolvedValue([
      { batch_id: 'b1', status: 'cancelled', total: 42, counts: { success: 15, cancelled: 26, failed: 1 },
        created_at: '2026-09-08T05:23:47+00:00', source: { type: 'credit_risk' } },
      { batch_id: 'b2', status: 'succeeded', total: 3, counts: { success: 3 },
        created_at: '2026-09-07T05:00:00+00:00', source: { type: 'credit_risk' } },
    ]);
    const onResumeBatch = vi.fn();
    const onOpenBatch = vi.fn();
    const { container } = render(<RunsPanel open graphId="g1" onClose={() => {}} onOpenRun={() => {}}
      onOpenBatch={onOpenBatch} onResumeBatch={onResumeBatch} />);
    const view = within(container);
    await userEvent.setup().click(await view.findByRole('button', { name: /batches/i }));

    const buttons = await view.findAllByRole('button', { name: /resume \(27 left\)/i });
    expect(buttons).toHaveLength(1);
    await userEvent.setup().click(buttons[0]);
    expect(onResumeBatch).toHaveBeenCalledWith(expect.objectContaining({ batch_id: 'b1' }));
    expect(onOpenBatch).not.toHaveBeenCalled();
  });
});
