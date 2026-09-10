import React from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';
import MemoryResourceInspector from './MemoryResourceInspector.jsx';
vi.mock('./Mem0Spaces.jsx', () => ({
  Mem0SpacePicker: () => <div>Choose a space</div>,
  Mem0Manager: () => <textarea aria-label="Draft memory" />,
}));
const resource = { id: 'mem:space:s', data: { kind: 'mem0', title: 'Research', space_id: 's' } };
const nodes = [{ id: 'researcher', data: { kind: 'task' } }];
const edges = ['read', 'write'].map(direction => ({ id: direction, data: { resource: resource.id, agent: 'researcher', memory: direction } }));
function setup(extra = {}) {
  const props = { resource, graphId: 'g', nodes, edges, onBack: vi.fn(), onConnect: vi.fn(), onDisconnect: vi.fn(), onEditAgent: vi.fn(), ...extra };
  render(<MemoryResourceInspector {...props} />);
  return { ...props, user: userEvent.setup() };
}
it('always provides a way back, including when adding a resource', async () => {
  const { user, onBack } = setup({ resource: null });
  await user.click(screen.getByRole('button', { name: '← Back to workflow' }));
  expect(onBack).toHaveBeenCalledOnce();
});
it('groups the two directions under one Agent and disconnects just one', async () => {
  const { user, onDisconnect } = setup();
  expect(screen.getAllByRole('button', { name: 'researcher →' })).toHaveLength(1);
  expect(screen.getByRole('checkbox', { name: 'researcher write' })).toBeChecked();
  await user.click(screen.getByRole('checkbox', { name: 'researcher read' }));
  expect(onDisconnect).toHaveBeenCalledWith(edges[0]);
});
it('returns from entries to connections without losing the draft, then exits', async () => {
  const { user, onBack } = setup();
  await user.click(screen.getByRole('button', { name: 'Browse and edit memories →' }));
  expect(screen.queryByRole('checkbox', { name: 'researcher read' })).not.toBeInTheDocument();
  await user.type(screen.getByLabelText('Draft memory'), 'Unsaved note');
  await user.click(screen.getByRole('button', { name: '← Back to memory' }));
  expect(screen.getByRole('checkbox', { name: 'researcher read' })).toBeVisible();
  await user.click(screen.getByRole('button', { name: 'Browse and edit memories →' }));
  expect(screen.getByLabelText('Draft memory')).toHaveValue('Unsaved note');
  await user.click(screen.getByRole('button', { name: '← Back to memory' }));
  await user.click(screen.getByRole('button', { name: '← Back to workflow' }));
  expect(onBack).toHaveBeenCalledOnce();
});
