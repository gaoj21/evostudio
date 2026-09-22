import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { beforeEach, expect, it, vi } from 'vitest';

vi.mock('../api.js', () => ({ api: { listReviews: vi.fn(), resolveReview: vi.fn() } }));
const { api } = await import('../api.js');
import ReviewPanel, { reviewFields } from './ReviewPanel.jsx';

beforeEach(() => {
  vi.clearAllMocks();
  api.resolveReview.mockResolvedValue({});
});

it('shows the rule-chosen fields and the rule outcome names on the buttons', async () => {
  api.listReviews.mockResolvedValue([{ review_id: 'r1', run_id: 'run-9', graph_id: 'g', status: 'pending', node: 'judge',
    score: 50, zone: [35, 65], fields: { verdict: 'escalate', why: 'unclear' }, output: { verdict: 'escalate', hidden: 'x' },
    approve_label: 'escalate', reject_label: 'close' }]);
  render(<ReviewPanel open onClose={vi.fn()} />);
  expect(await screen.findByText('judge · run run-9')).toBeInTheDocument();
  expect(screen.getByText('verdict:')).toBeInTheDocument();
  expect(screen.getByText(/unclear/)).toBeInTheDocument();
  expect(screen.queryByText(/hidden/)).not.toBeInTheDocument();
  await userEvent.setup().click(screen.getByRole('button', { name: 'Approve (escalate)' }));
  await waitFor(() => expect(api.resolveReview).toHaveBeenCalledWith('r1', 'approve', ''));
  expect(screen.getByRole('button', { name: 'Reject (close)' })).toBeInTheDocument();
  expect(document.body.textContent).not.toMatch(/company|severity|alert/i);
});

it('falls back to whatever keys an older review carries, generically', () => {
  const old = { review_id: 'o', status: 'approved', score: 40, zone: [35, 65], company: 'Acme', severity: 'high', summary: 'text',
    final_action: 'alert', created_at: 't' };
  expect(reviewFields(old)).toEqual([['company', 'Acme'], ['severity', 'high'], ['summary', 'text']]);
  expect(reviewFields({ fields: { a: 1, b: { c: 2 }, d: null } })).toEqual([['a', '1'], ['b', '{"c":2}']]);
});

it('uses neutral outcome names when a review has none', async () => {
  api.listReviews.mockResolvedValue([{ review_id: 'r2', status: 'pending', node: 'n', fields: {} }]);
  render(<ReviewPanel open onClose={vi.fn()} />);
  expect(await screen.findByRole('button', { name: 'Approve (approved)' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Reject (rejected)' })).toBeInTheDocument();
});
