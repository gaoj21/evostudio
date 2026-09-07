/**
 * The batch tab of the run dialog.
 *
 * Two things made it unpleasant: it asked for a file even when the graph
 * already said where its data comes from, and it never said how many runs you
 * were about to start — so `n=4` with monthly stepping quietly meant 24.
 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import RunDialog, { previewSummary } from './RunDialog.jsx';

vi.mock('../api.js', () => ({
  api: {
    graphInputs: vi.fn(),
    creditRiskSource: vi.fn(),
    listMetrics: vi.fn(),
    previewBatchCanvas: vi.fn(),
    previewBatchSource: vi.fn(),
    previewBatchUpload: vi.fn(),
    runBatchCanvas: vi.fn(),
    runBatchSource: vi.fn(),
    runBatchUpload: vi.fn(),
  },
}));

const { api } = await import('../api.js');

const STEPPED = {
  total: 24, samples: 4, steps: 6, steps_min: 6,
  dates: ['2025-03-31', '2025-08-31'], fields: ['company', 'news_batch'],
};

beforeEach(() => {
  vi.clearAllMocks();
  api.graphInputs.mockResolvedValue({ nodes: [], workflow_inputs: [], prefill: {} });
  api.creditRiskSource.mockResolvedValue({ splits: { test: 41 }, fields: ['company'] });
  api.listMetrics.mockResolvedValue({ metrics: [] });
  api.previewBatchCanvas.mockResolvedValue(STEPPED);
  api.previewBatchSource.mockResolvedValue({ total: 3, samples: 3, steps: 1, steps_min: 1 });
});

function open({ hasCanvasSource = true, beforeRun } = {}) {
  render(
    <RunDialog
      open
      graphId="g1"
      workflowInputs={[]}
      hasCanvasSource={hasCanvasSource}
      onCancel={vi.fn()}
      onSubmit={vi.fn()}
      beforeRun={beforeRun}
      onBatchStart={vi.fn()}
    />
  );
  return userEvent.setup();
}

// The dialog opens on the single-run tab; the batch form is behind it.
async function batchTab(user) {
  await user.click(await screen.findByRole('button', { name: /batch/i }));
}

describe('previewSummary', () => {
  it('says nothing clever when each record is its own run', () => {
    expect(previewSummary({ total: 4, samples: 4, steps: 1, steps_min: 1 }))
      .toBe('4 runs');
  });

  it('spells out that stepping multiplied the batch', () => {
    // 4 samples, 6 dates each. The number you typed was 4.
    expect(previewSummary(STEPPED)).toBe(
      '24 runs — 4 samples stepped over 6 dates each covering 2025-03-31 to 2025-08-31'
    );
  });

  it('reports a range when windows differ in length', () => {
    expect(previewSummary({ ...STEPPED, steps_min: 3 }))
      .toContain('3–6 dates each');
  });

  it('gets the singular right', () => {
    expect(previewSummary({ total: 1, samples: 1, steps: 1, steps_min: 1 }))
      .toBe('1 run');
  });

  it('has nothing to say before the count arrives', () => {
    expect(previewSummary(null)).toBeNull();
  });
});

describe('batch data source', () => {
  it('starts on the canvas source when the graph has one', async () => {
    // Asking for a file when the graph already declares its own feed is the
    // wrong question, and it was the default.
    const user = open();
    await batchTab(user);

    expect(await screen.findByLabelText(/data source/i)).toHaveValue('canvas');
    await waitFor(() => expect(api.previewBatchCanvas).toHaveBeenCalledWith('g1'));
  });

  it('still asks for a file when there is no source node', async () => {
    const user = open({ hasCanvasSource: false });
    await batchTab(user);

    expect(await screen.findByLabelText(/data source/i)).toHaveValue('upload');
    expect(api.previewBatchCanvas).not.toHaveBeenCalled();
  });

  it('saves the canvas first, so the count reflects what is on screen', async () => {
    // The source node's n / step live in the graph; counting the saved copy
    // of an edited canvas would report the old numbers.
    const beforeRun = vi.fn().mockResolvedValue();
    const user = open({ beforeRun });
    await batchTab(user);

    await waitFor(() => expect(beforeRun).toHaveBeenCalled());
    expect(api.previewBatchCanvas).toHaveBeenCalled();
  });

  it('previews with the new graph id when saving renamed it', async () => {
    const beforeRun = vi.fn().mockResolvedValue({ id: 'renamed-graph' });
    const user = open({ beforeRun });
    await batchTab(user);

    await waitFor(() => expect(api.previewBatchCanvas).toHaveBeenCalledWith('renamed-graph'));
  });
});

describe('how big this batch is', () => {
  it('shows the run count before you start', async () => {
    const user = open();
    await batchTab(user);

    expect(await screen.findByText(/24 runs/)).toBeInTheDocument();
  });

  it('puts the count on the button you are about to press', async () => {
    const user = open();
    await batchTab(user);

    expect(await screen.findByRole('button', { name: 'Run batch (24)' })).toBeInTheDocument();
  });

  it('warns when the batch is a large one', async () => {
    const user = open();
    await batchTab(user);

    expect(await screen.findByText(/This one is large/)).toBeInTheDocument();
  });

  it('does not warn about a small one', async () => {
    api.previewBatchCanvas.mockResolvedValue({ total: 4, samples: 4, steps: 1, steps_min: 1 });
    const user = open();
    await batchTab(user);

    await screen.findByText(/4 runs/);
    expect(screen.queryByText(/This one is large/)).not.toBeInTheDocument();
  });

  it('names the fields each run receives', async () => {
    const user = open();
    await batchTab(user);

    expect(await screen.findByText(/company, news_batch/)).toBeInTheDocument();
  });

  it('reports a source it cannot read instead of a count', async () => {
    api.previewBatchCanvas.mockRejectedValue({ body: { detail: 'no connected source node' } });
    const user = open();
    await batchTab(user);

    expect(await screen.findByText(/no connected source node/)).toBeInTheDocument();
    expect(screen.queryByText(/runs$/)).not.toBeInTheDocument();
  });

  it('asks for no count until a file is chosen', async () => {
    const user = open({ hasCanvasSource: false });
    await batchTab(user);

    await waitFor(() => expect(api.listMetrics).toHaveBeenCalled());
    expect(api.previewBatchUpload).not.toHaveBeenCalled();
  });
});

describe('stepping from the dialog', () => {
  // Stepping used to be reachable only by editing the source node on the
  // canvas, even though it is the setting that decides how big the batch is.
  async function creditRiskTab() {
    const user = open({ hasCanvasSource: false });
    await batchTab(user);
    await user.selectOptions(await screen.findByLabelText(/data source/i), 'credit_risk');
    return user;
  }

  it('offers it alongside split and seed', async () => {
    await creditRiskTab();
    expect(await screen.findByLabelText(/walk the window/i)).toHaveValue('none');
  });

  it('re-counts the batch when it changes', async () => {
    const user = await creditRiskTab();
    api.previewBatchSource.mockResolvedValue({
      total: 18, samples: 3, steps: 6, steps_min: 6, dates: ['2024-01-01', '2024-06-30'],
    });
    await user.selectOptions(screen.getByLabelText(/walk the window/i), 'monthly');

    await waitFor(() => expect(api.previewBatchSource).toHaveBeenCalledWith(
      'g1', expect.objectContaining({ step: 'monthly' })));
    expect(await screen.findByText(/18 runs — 3 samples stepped over 6 dates each/))
      .toBeInTheDocument();
  });

  it('runs with the same stepping it counted', async () => {
    // A preview that counted 18 and a run that does 3 is worse than no count.
    const user = await creditRiskTab();
    api.runBatchSource.mockResolvedValue({ batch_id: 'b1' });
    await user.selectOptions(screen.getByLabelText(/walk the window/i), 'weekly');
    await user.click(await screen.findByRole('button', { name: /run batch/i }));

    await waitFor(() => expect(api.runBatchSource).toHaveBeenCalledWith(
      'g1', expect.objectContaining({ step: 'weekly' })));
  });

  it('starts the batch with the new graph id returned by save', async () => {
    const beforeRun = vi.fn().mockResolvedValue({ id: 'renamed-graph' });
    api.runBatchCanvas.mockResolvedValue({ batch_id: 'b1' });
    const user = open({ beforeRun });
    await batchTab(user);
    await user.click(await screen.findByRole('button', { name: /run batch/i }));

    await waitFor(() => expect(api.runBatchCanvas).toHaveBeenCalledWith(
      'renamed-graph', expect.objectContaining({ workers: 2 })));
  });
});
