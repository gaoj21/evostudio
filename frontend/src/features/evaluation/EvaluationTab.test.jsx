import { render, within } from '@testing-library/react';
import React from 'react';
import { describe, expect, it } from 'vitest';
import EvaluationTab from './EvaluationTab.jsx';

const report = { status: 'success', metrics: { accuracy: 0.75 }, objective: { metric: 'accuracy', direction: 'maximize' },
  coverage: { unit: 'records', total: 4, scored: 4, unscored: 0 }, logs: 'checked 4 records\n' };

describe('a batch shows the reports its evaluations left', () => {
  it('shows each report, with the objective and printed output', () => {
    const { container } = render(<EvaluationTab batch={{ batch_id: 'b1', graph_id: 'g', status: 'succeeded', evaluations: { evaluation: report } }} />);
    const view = within(container);
    expect(view.getByText(/evaluation · success · accuracy 0.75/)).toBeInTheDocument();
    expect(view.getByTestId('evaluator-logs')).toHaveTextContent('checked 4 records');
    expect(view.queryByRole('button', { name: /evaluate/i })).toBeNull();   // evaluations are run in Evaluation & Evolve
  });

  it('says when no objective was chosen and the first metric was used', () => {
    const auto = { ...report, objective: { metric: 'accuracy', direction: 'maximize', chosen: 'first metric returned' } };
    const { container } = render(<EvaluationTab batch={{ batch_id: 'b1', graph_id: 'g', status: 'succeeded', evaluations: { evaluation: auto } }} />);
    expect(within(container).getByTestId('objective-note')).toHaveTextContent('No objective metric chosen yet');
  });

  it('says where an evaluation is run when there is none', () => {
    const { container } = render(<EvaluationTab batch={{ batch_id: 'b1', graph_id: 'g', status: 'succeeded' }} />);
    expect(within(container).getByTestId('no-evaluation')).toHaveTextContent('New run → Evaluation');
  });
});
