import { render, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../api.js', () => ({ api: { graphEvaluators: vi.fn(), runGraphEvaluator: vi.fn() } }));
const { api } = await import('../../api.js');
import EvaluationTab from './EvaluationTab.jsx';

const report = { status: 'success', metrics: { accuracy: 0.75 }, objective: { metric: 'accuracy', direction: 'maximize' },
  coverage: { unit: 'records', total: 4, scored: 4, unscored: 0 }, logs: 'checked 4 records\n' };

beforeEach(() => {
  vi.clearAllMocks();
  api.graphEvaluators.mockResolvedValue({ evaluators: [{ name: 'check', metric: 'accuracy', timing: 'manual' }] });
});

describe('a batch is evaluated by the workflow\'s saved evaluators', () => {
  it('shows the reports the batch already has, with the objective and printed output', async () => {
    const { container } = render(<EvaluationTab batch={{ batch_id: 'b1', graph_id: 'g', status: 'succeeded', evaluations: { check: report } }} />);
    const view = within(container);
    expect(view.getByText(/check · success · accuracy 0.75/)).toBeInTheDocument();
    expect(view.getByTestId('evaluator-logs')).toHaveTextContent('checked 4 records');
    await waitFor(() => expect(api.graphEvaluators).toHaveBeenCalled());
  });

  // The evaluator that produced them may have been migrated off the canvas
  // and never saved again: the report is still the batch's.
  it('shows past reports even when the workflow has no saved evaluator', async () => {
    api.graphEvaluators.mockResolvedValue({ evaluators: [] });
    const { container } = render(<EvaluationTab batch={{ batch_id: 'b1', graph_id: 'g', status: 'succeeded', evaluations: { gone: report } }} />);
    const view = within(container);
    expect(view.getByText(/gone · success · accuracy 0.75/)).toBeInTheDocument();
    await waitFor(() => expect(api.graphEvaluators).toHaveBeenCalledWith('g'));
    expect(view.queryByRole('button', { name: /run this evaluator/i })).not.toBeInTheDocument();
  });

  it('runs a saved evaluator on the saved results and shows what it returns', async () => {
    api.runGraphEvaluator.mockResolvedValue({ evaluations: { check: { status: 'success', ...report } } });
    const { container } = render(<EvaluationTab batch={{ batch_id: 'b1', graph_id: 'g', status: 'succeeded' }} />);
    const view = within(container);
    expect(view.getByTestId('no-evaluation')).toHaveTextContent('Evaluate & Evolve');
    await userEvent.setup().click(await view.findByRole('button', { name: /evaluate these results/i }));
    expect(api.runGraphEvaluator).toHaveBeenCalledWith('g', { name: 'check', batch_id: 'b1' });
    expect(await view.findByText(/accuracy 0.75/)).toBeInTheDocument();
  });

  it('shows an evaluator error with the line in the user code', async () => {
    api.runGraphEvaluator.mockRejectedValue({ body: { detail: "KeyError: 'x'\nIn your code:\n  line 4, in evaluate: total += r['x']" } });
    const { container } = render(<EvaluationTab batch={{ batch_id: 'b1', graph_id: 'g', status: 'succeeded' }} />);
    const view = within(container);
    await userEvent.setup().click(await view.findByRole('button', { name: /evaluate these results/i }));
    await waitFor(() => expect(view.getByRole('alert')).toHaveTextContent('line 4, in evaluate'));
  });

  it('says when no objective was chosen and the first metric was used', async () => {
    const auto = { ...report, objective: { metric: 'accuracy', direction: 'maximize', chosen: 'first metric returned' } };
    const { container } = render(<EvaluationTab batch={{ batch_id: 'b1', graph_id: 'g', status: 'succeeded', evaluations: { check: auto } }} />);
    expect(within(container).getByTestId('objective-note')).toHaveTextContent('No objective metric chosen yet');
    await waitFor(() => expect(api.graphEvaluators).toHaveBeenCalled());
  });

  it('waits for a running batch', async () => {
    const { container } = render(<EvaluationTab batch={{ batch_id: 'b1', graph_id: 'g', status: 'running' }} />);
    expect(await within(container).findByRole('button', { name: /evaluate these results/i })).toBeDisabled();
  });
});
