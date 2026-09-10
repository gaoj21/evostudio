import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { describe, expect, it, vi } from 'vitest';

import JsonView from './JsonView.jsx';

const decide = { decision: '{"action":"alert","risk_level":"high","score":70,"rationale":"Chapter 11 reports."}' };

describe('a node output reads as a tree, not an escaped string', () => {
  it('shows the keys inside the string, without the backslashes', () => {
    render(<JsonView value={decide} />);
    expect(screen.getByText('action')).toBeInTheDocument();
    expect(screen.getByText('risk_level')).toBeInTheDocument();
    expect(screen.getByText('70')).toBeInTheDocument();
    expect(screen.getByTestId('json-view').textContent).not.toContain('\\"');
  });

  it('folds an object down to a count and opens it again', async () => {
    render(<JsonView value={decide} />);
    const user = userEvent.setup();
    // The inner object: second fold button (the first is the root).
    const folds = screen.getAllByRole('button', { name: /collapse/i });
    await user.click(folds[1]);
    expect(screen.getByText(/4 keys/)).toBeInTheDocument();
    expect(screen.queryByText('risk_level')).not.toBeInTheDocument();
    await user.click(screen.getByText(/4 keys/));
    expect(screen.getByText('risk_level')).toBeInTheDocument();
  });

  it('switches to raw text, which is the unfolded JSON', async () => {
    render(<JsonView value={decide} />);
    await userEvent.setup().click(screen.getByRole('button', { name: 'Raw' }));
    const raw = screen.getByTestId('json-view').textContent;
    expect(raw).toContain('"action": "alert"');
    expect(raw).not.toContain('\\"');
  });

  it('copies the unfolded JSON', async () => {
    // user-event installs its own clipboard stub at setup; spy on that one.
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue();
    render(<JsonView value={decide} />);
    await user.click(screen.getByRole('button', { name: 'Copy' }));
    expect(writeText).toHaveBeenCalledWith(expect.stringContaining('"score": 70'));
    expect(await screen.findByText('Copied')).toBeInTheDocument();
  });

  it('cuts a long array and shows the rest on request', async () => {
    const items = Array.from({ length: 25 }, (_, i) => ({ id: i }));
    render(<JsonView value={items} />);
    expect(screen.queryByText('24')).not.toBeInTheDocument();
    await userEvent.setup().click(screen.getByText(/5 more/));
    expect(screen.getByText('24')).toBeInTheDocument();
  });

  it('shows plain text as plain text, with no toolbar', () => {
    render(<JsonView value="Nothing to report." />);
    expect(screen.getByText('Nothing to report.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Raw' })).not.toBeInTheDocument();
  });
});
