import { render, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../api.js', () => ({
  api: { listMemoryAgents: vi.fn(), searchMemory: vi.fn() },
}));
const { api } = await import('../../api.js');
import MemoryPanel from './MemoryPanel.jsx';

const ROWS = [
  { subject: 'Lucid', at: '2026-01-16', recorded_at: '2026-09-08T06:00:00', content: { outputs: { decision: '{"action":"suppress","score":15}' } } },
  { subject: 'Sleep Number', at: '2026-02-11', recorded_at: '2026-09-08T06:00:00', content: { outputs: { decision: '{"action":"suppress","score":0}' } } },
  { subject: 'Sleep Number', at: '2026-03-13', recorded_at: '2026-09-08T06:03:00', content: { outputs: { decision: '{"action":"alert","score":82}' } } },
];

beforeEach(() => {
  api.listMemoryAgents.mockResolvedValue({ agents: [], stores: [
    { node: 'decide', kind: 'table', count: 3, subjects: ['Lucid', 'Sleep Number'] },
    { node: 'investigate', kind: 'table', count: 3, subjects: ['Lucid', 'Sleep Number'] },
  ] });
  api.searchMemory.mockImplementation((g, node, q) => Promise.resolve({
    kind: 'table',
    entries: (q ? ROWS.filter((r) => r.subject === q) : ROWS).map((r) => (node === 'investigate'
      ? { ...r, content: { outputs: { context: `{"profile":"${r.subject} profile"}` } } }
      : r)),
  }));
});

describe('table memory reads as a timeline per company', () => {
  it('lists the table stores with their size, not "(no memory stores yet)"', async () => {
    const { container } = render(<MemoryPanel graphId="g1" />);
    const view = within(container);
    expect(await view.findByRole('option', { name: /decide · table · 3 rows/ })).toBeInTheDocument();
    expect(view.queryByText(/no memory stores yet/)).not.toBeInTheDocument();
  });

  it('opens on both tables at once, one row per date with a cell per node', async () => {
    const { container } = render(<MemoryPanel graphId="g1" />);
    const view = within(container);
    expect(await view.findByRole('option', { name: /all tables · decide \+ investigate/ })).toBeInTheDocument();
    expect(view.getByLabelText('Memory store')).toHaveValue('__all__');
    await userEvent.setup().click(await view.findByRole('button', { name: /Sleep Number/ }));
    // Two dates, and under each date both nodes.
    expect(view.getAllByText(/^2026-\d\d-\d\d$/)).toHaveLength(2);
    expect(view.getAllByText('investigate')).toHaveLength(2);
    expect(view.getAllByText('decide')).toHaveLength(2);
    expect(view.getByText(/alert · 82/)).toBeInTheDocument();
    expect(view.getAllByText(/Sleep Number profile/)).toHaveLength(2);
  });

  it('starts folded to one heading per company, with the span of dates', async () => {
    const { container } = render(<MemoryPanel graphId="g1" />);
    const view = within(container);
    expect(await view.findByText('Sleep Number')).toBeInTheDocument();
    expect(view.getByText(/2 states/)).toBeInTheDocument();
    expect(view.getAllByText(/2026-02-11 → 2026-03-13/).length).toBeGreaterThan(0);
    expect(view.queryByText('alert · 82')).not.toBeInTheDocument();
  });

  it('unfolds a company into its dated states, each with a one-line summary', async () => {
    const { container } = render(<MemoryPanel graphId="g1" />);
    const view = within(container);
    await userEvent.setup().click(await view.findByRole('button', { name: /Sleep Number/ }));
    const dates = view.getAllByText(/^2026-\d\d-\d\d$/).map((el) => el.textContent);
    expect(dates).toEqual(['2026-02-11', '2026-03-13']);
    expect(view.getByText(/alert · 82/)).toBeInTheDocument();   // JSON-in-string unfolded
    expect(view.queryByText('2026-01-16')).not.toBeInTheDocument();  // Lucid still folded
  });

  it('expands and collapses everything at once', async () => {
    const { container } = render(<MemoryPanel graphId="g1" />);
    const view = within(container);
    const user = userEvent.setup();
    await user.click(await view.findByRole('button', { name: 'Expand all' }));
    expect(view.getAllByText(/^2026-\d\d-\d\d$/)).toHaveLength(3);   // dates, not cells
    await user.click(view.getByRole('button', { name: 'Collapse all' }));
    expect(view.queryAllByText(/^2026-\d\d-\d\d$/)).toHaveLength(0);
  });

  it('narrows to one company', async () => {
    const { container } = render(<MemoryPanel graphId="g1" />);
    const view = within(container);
    await view.findByText('Lucid');
    await userEvent.setup().selectOptions(view.getByLabelText('Subject'), 'Sleep Number');
    expect(await view.findByText(/2 states/)).toBeInTheDocument();
    // "Lucid" stays in the subject dropdown; it must be gone from the list.
    expect(view.queryByText(/1 state$/)).not.toBeInTheDocument();
    expect(api.searchMemory).toHaveBeenCalledWith('g1', 'decide', 'Sleep Number');
    expect(api.searchMemory).toHaveBeenCalledWith('g1', 'investigate', 'Sleep Number');
  });

  it('reloads on Refresh', async () => {
    const { container } = render(<MemoryPanel graphId="g1" />);
    const view = within(container);
    await view.findByText('Lucid');
    const before = api.searchMemory.mock.calls.length;
    await userEvent.setup().click(view.getByRole('button', { name: 'Refresh' }));
    expect(api.searchMemory.mock.calls.length).toBeGreaterThan(before);
  });
});
