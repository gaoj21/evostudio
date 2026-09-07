import { render, screen, within } from '@testing-library/react';
import React from 'react';
import { describe, expect, it, vi } from 'vitest';

import BatchCompare, { deltaClass, signed } from './BatchCompare.jsx';

const COMPARISON = {
  baseline: { batch_id: 'b1', metric: 'exact_match', mean: 0.62, total: 8 },
  candidate: { batch_id: 'b2', metric: 'exact_match', mean: 0.88, total: 8 },
  matched: 8, mean_before: 0.625, mean_after: 0.875, delta: 0.25,
  improved: 3, regressed: 1, unchanged: 4, unscored: 0, notes: [],
  changes: [
    { label: 'Oslo', delta: -1, score_before: 1, score_after: 0,
      status_before: 'success', status_after: 'success', output_after: '{"answer": "?"}' },
    { label: 'Cairo', delta: 1, score_before: 0, score_after: 1,
      status_before: 'success', status_after: 'success', output_after: '{"answer": "Cairo"}' },
    { label: 'Lima', delta: 0, score_before: 1, score_after: 1,
      status_before: 'success', status_after: 'success' },
  ],
};

const show = (extra = {}) =>
  render(<BatchCompare comparison={{ ...COMPARISON, ...extra }} onBack={vi.fn()} />);

describe('signed', () => {
  it('keeps the sign, which is the whole message', () => {
    expect(signed(0.25)).toBe('+0.25');
    expect(signed(-0.25)).toBe('−0.25');   // a real minus sign, not a hyphen
  });

  it('does not dress up no change as an improvement', () => {
    expect(signed(0)).toBe('0.00');
    expect(signed(0.0001)).toBe('0.00');
  });

  it('shows a dash where there is no number rather than "+0.00"', () => {
    expect(signed(null)).toBe('—');
    expect(signed(undefined)).toBe('—');
  });
});

describe('deltaClass', () => {
  it('separates better, worse, and neither', () => {
    expect(deltaClass(0.1)).toBe('delta-up');
    expect(deltaClass(-0.1)).toBe('delta-down');
    expect(deltaClass(0)).toBe('delta-flat');
    expect(deltaClass(null)).toBe('delta-none');
  });
});

describe('BatchCompare', () => {
  it('leads with the movement in the mean', () => {
    show();
    expect(screen.getByText('0.63')).toBeInTheDocument();
    expect(screen.getByText('0.88')).toBeInTheDocument();
    expect(screen.getByText('+0.25')).toHaveClass('delta-up');
    expect(screen.getByText(/exact_match over 8 shared records/)).toBeInTheDocument();
  });

  it('puts the regression above the improvements', () => {
    show();
    const rows = document.querySelectorAll('.compare-row');
    // A mean that went up is exactly when the one that went down is easiest
    // to miss, so it is not buried below three green rows.
    expect(within(rows[0]).getByText('Oslo')).toBeInTheDocument();
    expect(within(rows[0]).getByText('−1.00')).toHaveClass('delta-down');
  });

  it('leaves out the records that did not move', () => {
    show();
    expect(screen.queryByText('Lima')).not.toBeInTheDocument();
    expect(screen.getByText('4 unchanged')).toBeInTheDocument();
  });

  it('shows what the changed record produced', () => {
    show();
    expect(screen.getByText('{"answer": "?"}')).toBeInTheDocument();
  });

  it('says so when nothing moved at all', () => {
    show({ changes: [COMPARISON.changes[2]], improved: 0, regressed: 0, unchanged: 1 });
    expect(screen.getByText('Every shared record scored exactly the same.')).toBeInTheDocument();
  });

  it('surfaces a warning that the two are not comparable', () => {
    show({ notes: ['These were scored with different metrics (exact_match vs f1); '
      + 'the scores are not comparable.'] });
    expect(screen.getByText(/not comparable/)).toBeInTheDocument();
  });

  it('does not invent a comparison when the two share no records', () => {
    show({
      matched: 0, mean_before: null, mean_after: null, delta: null,
      improved: 0, regressed: 0, unchanged: 0, changes: [],
      notes: ['No record appears in both batches — they were run over different inputs.'],
    });
    // Dashes, not zeroes: "0.00 → 0.00 +0.00" would read as a real result.
    expect(screen.getAllByText('—')).toHaveLength(3);
    expect(screen.getByText(/No record appears in both/)).toBeInTheDocument();
  });

  it('marks a record that stopped scoring rather than calling it a regression', () => {
    show({
      changes: [{ label: 'Rome', delta: null, score_before: 1, score_after: null,
                  status_before: 'success', status_after: 'failed' }],
      improved: 0, regressed: 0, unchanged: 0, unscored: 1,
    });
    expect(screen.getByText('—')).toHaveClass('delta-none');
    expect(screen.getByText(/1.00 → — · failed/)).toBeInTheDocument();
    expect(screen.getByText('1 unscored')).toBeInTheDocument();
  });

  it('goes back to the list', async () => {
    const onBack = vi.fn();
    render(<BatchCompare comparison={COMPARISON} onBack={onBack} />);
    screen.getByRole('button', { name: '← Back to batches' }).click();
    expect(onBack).toHaveBeenCalled();
  });
});
