import { render, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../api.js', () => ({
  api: { getBatchEvaluation: vi.fn(), evaluateBatch: vi.fn(), listMetrics: vi.fn() },
}));
const { api } = await import('../../api.js');
import EvaluationTab from './EvaluationTab.jsx';

const REPORT = {
  kind: 'credit_risk', batch_status: 'completed_with_errors',
  headline: 'detected 1/1 · false alarms 0/1 · lead 30d',
  all: { positives: 2, detected: 1, negatives: 1, false_alarms: 0, mean_lead_days: 30, weeks: 5, weeks_exact: 2, weeks_near: 4, exact_rate: 0.4, near_rate: 0.8 },
  unflagged: { positives: 1, detected: 1, negatives: 1, false_alarms: 0, mean_lead_days: 30, weeks: 4, weeks_exact: 2, weeks_near: 4, exact_rate: 0.5, near_rate: 1 },
  flagged: [{ company: 'Silver', flags: ['generic_name'] }],
  failed_steps: 1,
  companies: [
    { sample_id: 'a', company: 'Acme', type: 'positive', event_date: '2026-05-01', flags: [], detected: true, first_alert: '2026-04-01', lead_days: 30,
      weeks: 2, weeks_exact: 1, weeks_near: 2, failed_steps: 0,
      steps: [{ as_of: '2026-03-01', status: 'success', action: 'suppress', risk_level: 'medium', score: 40, expected: 'high', match: 'near', rationale: 'watch' },
              { as_of: '2026-04-01', status: 'success', action: 'alert', risk_level: 'high', score: 70, expected: 'critical', match: 'near', rationale: 'going concern' }] },
    { sample_id: 's', company: 'Silver', type: 'positive', event_date: '2026-05-01', flags: ['generic_name'], detected: false, first_alert: null, lead_days: null,
      weeks: 1, weeks_exact: 0, weeks_near: 0, failed_steps: 0,
      steps: [{ as_of: '2026-04-01', status: 'success', action: 'suppress', risk_level: 'low', score: 0, expected: 'critical', match: 'off' }] },
    { sample_id: 'b', company: 'Beta', type: 'negative', event_date: null, flags: [], detected: null, false_alarm: false, first_alert: null, lead_days: null,
      weeks: 2, weeks_exact: 1, weeks_near: 2, failed_steps: 1,
      steps: [{ as_of: '2026-03-01', status: 'success', action: 'suppress', risk_level: 'low', score: 5, expected: 'low', match: 'exact' },
              { as_of: '2026-04-01', status: 'failed', action: null, risk_level: null, score: null, expected: 'low', match: null }] },
  ],
};

describe('a credit-risk batch is evaluated with one press', () => {
  beforeEach(() => {
    api.getBatchEvaluation.mockResolvedValue({ evaluation: null, credit_risk: true });
    api.evaluateBatch.mockResolvedValue(REPORT);
  });

  it('asks for nothing and shows the tallies and one row per company', async () => {
    const { container } = render(<EvaluationTab batch={{ batch_id: 'b1', status: 'completed_with_errors' }} />);
    const view = within(container);
    await view.findByText(/carry their own truth/);
    expect(view.queryByLabelText('Metric')).not.toBeInTheDocument();
    await userEvent.setup().click(view.getByRole('button', { name: 'Evaluate' }));
    expect(api.evaluateBatch).toHaveBeenCalledWith('b1', undefined);
    expect(await view.findByText('caught', { selector: '.eval-good' })).toBeInTheDocument();
    expect(view.getByText('missed', { selector: '.eval-bad' })).toBeInTheDocument();
    expect(view.getByText('clean', { selector: '.eval-good' })).toBeInTheDocument();
    expect(view.getByText('30d')).toBeInTheDocument();
    expect(view.getByText(/Without flagged samples \(1 excluded\)/)).toBeInTheDocument();
    expect(view.getByText(/1 step\(s\) did not finish/)).toBeInTheDocument();
  });

  it('opens a company row into its steps with rationale', async () => {
    const { container } = render(<EvaluationTab batch={{ batch_id: 'b1', status: 'completed_with_errors' }} />);
    const view = within(container);
    await view.findByText(/carry their own truth/);
    const user = userEvent.setup();
    await user.click(view.getByRole('button', { name: 'Evaluate' }));
    await user.click(await view.findByText('Acme'));
    expect(view.getByText('going concern')).toBeInTheDocument();
  });

  it('shows a stored report straight away and offers to redo it', async () => {
    api.getBatchEvaluation.mockResolvedValue({ evaluation: REPORT, credit_risk: true });
    const { container } = render(<EvaluationTab batch={{ batch_id: 'b1', status: 'completed_with_errors' }} />);
    const view = within(container);
    expect(await view.findByText('caught', { selector: '.eval-good' })).toBeInTheDocument();
    expect(view.getByRole('button', { name: 'Evaluate again' })).toBeInTheDocument();
  });
});

describe('any other batch picks a metric and a field', () => {
  it('sends the metric and label field', async () => {
    api.getBatchEvaluation.mockResolvedValue({ evaluation: null, credit_risk: false });
    api.listMetrics.mockResolvedValue({ metrics: [{ name: 'exact_match' }, { name: 'contains' }] });
    api.evaluateBatch.mockResolvedValue({ kind: 'metric', metric: 'contains', label_key: 'answer',
      summary: { mean: 0.5, scored: 2, total: 2, perfect: 1, min: 0, max: 1 }, items: [
        { index: 0, status: 'success', label: '2', score: 1 }, { index: 1, status: 'success', label: '4', score: 0 }] });
    const { container } = render(<EvaluationTab batch={{ batch_id: 'b2', status: 'succeeded' }} />);
    const view = within(container);
    const user = userEvent.setup();
    await user.selectOptions(await view.findByLabelText('Metric'), 'contains');
    await user.type(view.getByLabelText('Field holding the expected answer'), 'answer');
    await user.click(view.getByRole('button', { name: 'Evaluate' }));
    expect(api.evaluateBatch).toHaveBeenCalledWith('b2', { metric: 'contains', label_key: 'answer' });
    expect(await view.findByText('mean')).toBeInTheDocument();
    expect(view.getByText(/score 1/)).toBeInTheDocument();
  });
});
