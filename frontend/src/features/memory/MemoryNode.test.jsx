import React from 'react';
import { render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import MemoryNode from './MemoryNode.jsx';
vi.mock('@xyflow/react', () => ({
  Position: { Top: 'top', Right: 'right', Bottom: 'bottom', Left: 'left' },
  Handle: ({ id, type, position }) => <span data-testid={`${position}-${type}`} data-handle-id={id} />,
}));
it('offers read and write handles on every side with unique IDs and legacy top IDs', () => {
  render(<MemoryNode data={{ title: 'Shared memory', kind: 'mem0', keeps: [] }} />);
  const ids = [];
  for (const side of ['top', 'right', 'bottom', 'left']) {
    for (const type of ['source', 'target']) ids.push(screen.getByTestId(`${side}-${type}`).dataset.handleId);
  }
  expect(new Set(ids).size).toBe(8);
  expect(screen.getByTestId('top-target')).toHaveAttribute('data-handle-id', 's-in');
  expect(screen.getByTestId('top-source')).toHaveAttribute('data-handle-id', 's-out');
});
