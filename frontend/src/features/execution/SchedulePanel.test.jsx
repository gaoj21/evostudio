import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import SchedulePanel, { untilText } from './SchedulePanel.jsx';

vi.mock('../../api.js', () => ({
  api: { getSchedule: vi.fn(), setSchedule: vi.fn(), clearSchedule: vi.fn(), resumeSchedule: vi.fn() },
}));

const { api } = await import('../../api.js');

const NONE = { graph_id: 'probe', scheduled: false };
const DAILY = {
  graph_id: 'probe', scheduled: true, enabled: true, running: true,
  mode: 'daily', time: '09:00', inputs: { city: 'Lima' }, session: 'daily-report',
  fires: 4, next_fire: new Date(Date.now() + 3 * 3600_000).toISOString(),
  last_error: null,
};

function setup(schedule = NONE) {
  api.getSchedule.mockResolvedValue(schedule);
  api.setSchedule.mockResolvedValue({ ...DAILY, ...schedule, scheduled: true });
  render(<SchedulePanel open graphId="probe" onClose={vi.fn()} />);
  return userEvent.setup();
}

beforeEach(() => {
  api.clearSchedule.mockResolvedValue({ ok: true, removed: true });
});

describe('untilText', () => {
  const now = Date.parse('2026-09-06T12:00:00Z');
  it('answers the only question the timestamp is there for', () => {
    expect(untilText('2026-09-06T12:40:00Z', now)).toBe('in 40m');
    expect(untilText('2026-09-06T15:20:00Z', now)).toBe('in 3h 20m');
    expect(untilText('2026-09-08T12:00:00Z', now)).toBe('in 2d');
  });

  it('says so when a fire is late rather than showing a negative', () => {
    expect(untilText('2026-09-06T11:00:00Z', now)).toBe('overdue');
  });

  it('copes with no schedule and with nonsense', () => {
    expect(untilText(null)).toBe('');
    expect(untilText('not a date')).toBe('');
  });
});

describe('SchedulePanel', () => {
  it('offers to create one when there is none', async () => {
    setup();
    expect(await screen.findByRole('button', { name: 'Schedule it' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Pause' })).not.toBeInTheDocument();
  });

  it('says when the next run is and how many there have been', async () => {
    setup(DAILY);
    expect(await screen.findByText(/Next run in 2h 59m|Next run in 3h 0m/))
      .toBeInTheDocument();
    expect(screen.getByText(/4 so far/)).toBeInTheDocument();
  });

  it('fills the form in from the existing schedule', async () => {
    setup(DAILY);
    await waitFor(() => expect(screen.getByLabelText('Time')).toHaveValue('09:00'));
    expect(screen.getByDisplayValue('daily-report')).toBeInTheDocument();
    expect(screen.getByDisplayValue(/"city": "Lima"/)).toBeInTheDocument();
  });

  it('sets a daily schedule', async () => {
    const user = setup();
    await screen.findByRole('button', { name: 'Schedule it' });
    fireEvent.change(screen.getByLabelText('Time'), { target: { value: '07:30' } });
    await user.click(screen.getByRole('button', { name: 'Schedule it' }));

    await waitFor(() => expect(api.setSchedule).toHaveBeenCalledWith('probe',
      expect.objectContaining({ enabled: true, mode: 'daily', time: '07:30' })));
  });

  it('switches to an interval, with the floor spelled out', async () => {
    const user = setup();
    await screen.findByRole('button', { name: 'Schedule it' });
    await user.selectOptions(screen.getByLabelText(/How often/), 'interval');

    expect(screen.getByText(/At least 5 — nobody is watching these run/))
      .toBeInTheDocument();
  });

  it('refuses to send inputs that are not JSON', async () => {
    const user = setup();
    await screen.findByRole('button', { name: 'Schedule it' });
    fireEvent.change(screen.getByLabelText(/Inputs/), { target: { value: '{oops' } });
    await user.click(screen.getByRole('button', { name: 'Schedule it' }));

    expect(await screen.findByText(/Inputs must be JSON/)).toBeInTheDocument();
    expect(api.setSchedule).not.toHaveBeenCalled();
  });

  it('pauses without throwing the schedule away', async () => {
    const user = setup(DAILY);
    await user.click(await screen.findByRole('button', { name: 'Pause' }));

    await waitFor(() => expect(api.setSchedule).toHaveBeenCalledWith('probe',
      expect.objectContaining({ enabled: false })));
    expect(api.clearSchedule).not.toHaveBeenCalled();
  });

  it('removes one', async () => {
    const user = setup(DAILY);
    await user.click(await screen.findByRole('button', { name: 'Remove' }));

    await waitFor(() => expect(api.clearSchedule).toHaveBeenCalledWith('probe'));
    expect(await screen.findByRole('button', { name: 'Schedule it' })).toBeInTheDocument();
  });

  it('surfaces a refusal from the server', async () => {
    const user = setup();
    await screen.findByRole('button', { name: 'Schedule it' });
    // After setup: it installs a resolving mock of its own.
    api.setSchedule.mockRejectedValue({ body: { detail: 'must be at least 5' } });
    await user.click(screen.getByRole('button', { name: 'Schedule it' }));

    expect(await screen.findByText(/must be at least 5/)).toBeInTheDocument();
  });

  it('shows why the last fire failed', async () => {
    setup({ ...DAILY, last_error: 'no LLM configured' });
    expect(await screen.findByText(/last failed: no LLM configured/)).toBeInTheDocument();
  });

  it('fetches nothing while closed', () => {
    render(<SchedulePanel open={false} graphId="probe" onClose={vi.fn()} />);
    expect(api.getSchedule).not.toHaveBeenCalled();
  });
});


it('does not carry one workflow schedule into another that has none', async () => {
  api.getSchedule.mockImplementation(async (id) => id === 'A'
    ? { ...DAILY, graph_id: 'A', mode: 'interval', interval_minutes: 15, inputs: { secret: 'A-only' }, session: 'a-sess' }
    : { graph_id: 'B', scheduled: false });
  const { rerender } = render(<SchedulePanel open graphId="A" onClose={vi.fn()} />);
  await waitFor(() => expect(screen.getByLabelText(/Inputs/).value).toContain('A-only'));
  rerender(<SchedulePanel open={false} graphId="A" onClose={vi.fn()} />);
  rerender(<SchedulePanel open graphId="B" onClose={vi.fn()} />);
  await waitFor(() => expect(api.getSchedule).toHaveBeenCalledWith('B'));
  await waitFor(() => expect(screen.getByLabelText(/Inputs/).value).toBe('{}'));
  expect(screen.getByLabelText(/Session/).value).toBe('');
});


it('resumes the existing experiment with the selected catch-up strategy', async () => {
  api.resumeSchedule.mockResolvedValue({ ...DAILY, experiment_id: 'same-experiment' });
  const user = setup({ ...DAILY, running: false, needs_resume: true, experiment_id: 'same-experiment' });
  await screen.findByRole('button', { name: 'Resume experiment' });
  await user.selectOptions(screen.getByLabelText('Resume strategy'), 'latest');
  await user.click(screen.getByRole('button', { name: 'Resume experiment' }));
  expect(api.resumeSchedule).toHaveBeenCalledWith('probe', 'latest');
  expect(api.setSchedule).not.toHaveBeenCalled();
});

it('configures a weekly Monday schedule and recovery policy', async () => {
  const user = setup();
  await screen.findByRole('button', { name: 'Schedule it' });
  await user.selectOptions(screen.getByLabelText('How often'), 'weekly');
  await user.selectOptions(screen.getByLabelText('Day'), '0');
  await user.selectOptions(screen.getByLabelText('After downtime'), 'all');
  await user.click(screen.getByRole('button', { name: 'Schedule it' }));
  expect(api.setSchedule).toHaveBeenCalledWith('probe', expect.objectContaining({ mode: 'weekly', weekday: 0, recovery_policy: 'all' }));
});
