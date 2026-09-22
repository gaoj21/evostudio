import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';
import Mem0Spaces, { Mem0SpacePicker } from './Mem0Spaces.jsx';
import { api } from '../../api.js';
vi.mock('../../api.js', () => ({ api: {
  mem0Spaces: vi.fn(), createMem0Space: vi.fn(), mem0Entries: vi.fn(),
  addMem0Entry: vi.fn(), updateMem0Entry: vi.fn(), deleteMem0Entry: vi.fn(),
} }));
beforeEach(() => {
  vi.clearAllMocks();
  api.mem0Spaces.mockResolvedValue({ spaces: [{ id: 's1', name: 'Shared research' }] });
  api.mem0Entries.mockResolvedValue({ entries: [] });
  api.createMem0Space.mockResolvedValue({ id: 's2', name: 'New space' });
  api.addMem0Entry.mockResolvedValue({});
  api.updateMem0Entry.mockResolvedValue({});
});
it('creates and selects a space without writing any memories', async () => {
  const onChange = vi.fn(); const user = userEvent.setup();
  render(<Mem0SpacePicker graphId="g1" onChange={onChange} />);
  await user.type(screen.getByLabelText('New Mem0 space name'), 'New space');
  await user.click(screen.getByRole('button', { name: 'Create space' }));
  expect(api.createMem0Space).toHaveBeenCalledWith('g1', 'New space');
  expect(onChange).toHaveBeenCalledWith('s2');
  expect(api.addMem0Entry).not.toHaveBeenCalled();
});
async function open() {
  const user = userEvent.setup();
  const { container } = render(<Mem0Spaces graphId="g1" />);
  const details = container.querySelector('details'); details.open = true; fireEvent(details, new Event('toggle'));
  await screen.findByRole('option', { name: 'Shared research' });
  await user.selectOptions(screen.getByLabelText('Mem0 space'), 's1');
  await screen.findByText('No memories found.');
  return user;
}
it('writes manual material to the selected shared space', async () => {
  const user = await open();
  await user.type(screen.getByLabelText('Memory content'), 'New evidence');
  await user.click(screen.getByRole('button', { name: 'Add memory' }));
  expect(api.addMem0Entry).toHaveBeenCalledWith('g1', 's1', 'New evidence');
  await waitFor(() => expect(screen.getByLabelText('Memory content')).toHaveValue(''));
});
it('preserves unsaved material when the backend is unavailable', async () => {
  api.addMem0Entry.mockRejectedValue(new Error('Mem0 unavailable'));
  const user = await open();
  await user.type(screen.getByLabelText('Memory content'), 'Keep my draft');
  await user.click(screen.getByRole('button', { name: 'Add memory' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Mem0 unavailable');
  expect(screen.getByLabelText('Memory content')).toHaveValue('Keep my draft');
});
