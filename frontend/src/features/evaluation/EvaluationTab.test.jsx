import { render, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../api.js', () => ({ api: { evaluateCanvasResults: vi.fn() } }));
const { api } = await import('../../api.js');
import EvaluationTab from './EvaluationTab.jsx';

const report = { status: 'success', metrics: { accuracy: 0.75 }, objective: { metric: 'accuracy', direction: 'maximize' },
  coverage: { unit: 'records', total: 4, scored: 4, unscored: 0 }, logs: 'checked 4 records\n' };

beforeEach(() => vi.clearAllMocks());

describe('a batch is evaluated by the canvas evaluators', () => {
  it('shows the reports the batch already has, with the objective and printed output', () => {
    const { container } = render(<EvaluationTab batch={{ batch_id: 'b1', graph_id: 'g', status: 'succeeded', evaluations: { check: report } }} />);
    const view = within(container);
    expect(view.getByText(/check · success · accuracy 0.75/)).toBeInTheDocument();
    expect(view.getByTestId('evaluator-logs')).toHaveTextContent('checked 4 records');
  });

  it('runs the evaluators on the saved results and shows what they return', async () => {
    api.evaluateCanvasResults.mockResolvedValue({ evaluations: { check: report } });
    const { container } = render(<EvaluationTab batch={{ batch_id: 'b1', graph_id: 'g', status: 'succeeded' }} />);
    const view = within(container);
    expect(view.getByTestId('no-evaluation')).toHaveTextContent('drop your Python code');
    await userEvent.setup().click(view.getByRole('button', { name: /evaluate with the canvas evaluators/i }));
    expect(api.evaluateCanvasResults).toHaveBeenCalledWith('g', { batch_id: 'b1' });
    expect(await view.findByText(/accuracy 0.75/)).toBeInTheDocument();
  });

  it('shows an evaluator error with the line in the user code', async () => {
    api.evaluateCanvasResults.mockResolvedValue({ evaluations: { check: { status: 'failed', error: "KeyError: 'x'\nIn your code:\n  line 4, in evaluate: total += r['x']" } } });
    const { container } = render(<EvaluationTab batch={{ batch_id: 'b1', graph_id: 'g', status: 'succeeded' }} />);
    const view = within(container);
    await userEvent.setup().click(view.getByRole('button', { name: /evaluate with the canvas evaluators/i }));
    await waitFor(() => expect(view.getByRole('alert')).toHaveTextContent('line 4, in evaluate'));
  });

  it('says when no objective was chosen and the first metric was used', () => {
    const auto = { ...report, objective: { metric: 'accuracy', direction: 'maximize', chosen: 'first metric returned' } };
    const { container } = render(<EvaluationTab batch={{ batch_id: 'b1', graph_id: 'g', status: 'succeeded', evaluations: { check: auto } }} />);
    expect(within(container).getByTestId('objective-note')).toHaveTextContent('No objective metric chosen yet');
  });

  it('waits for a running batch', () => {
    const { container } = render(<EvaluationTab batch={{ batch_id: 'b1', graph_id: 'g', status: 'running' }} />);
    expect(within(container).getByRole('button', { name: /evaluate with the canvas evaluators/i })).toBeDisabled();
  });
});
