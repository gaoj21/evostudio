import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import ChatAgentNode from './ChatAgentNode.jsx';
vi.mock('@xyflow/react', () => ({
  Position: { Top: 'top', Right: 'right', Bottom: 'bottom', Left: 'left' },
  Handle: ({ id, type, position }) => <span data-testid={`${position}-${type}`} data-handle-id={id} />,
}));
it('matches Agent port layout, shows its node name and deletes without opening the inspector', () => {
  const remove = vi.fn(), select = vi.fn();
  render(<div onClick={select}><ChatAgentNode id="research_chat" data={{ kind: 'chat_agent', onDelete: remove }} /></div>);
  for (const port of ['left-target', 'right-source', 'top-target', 'bottom-target', 'bottom-source']) expect(screen.getByTestId(port)).toBeInTheDocument();
  expect(screen.getByTestId('left-target')).toHaveAttribute('data-handle-id', 'in');
  expect(screen.getByTestId('right-source')).toHaveAttribute('data-handle-id', 'out');
  expect(screen.getByText('☏ research_chat')).toBeInTheDocument();
  expect(screen.getByText('Independent chat · not part of runs')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Delete Chat Agent' }));
  expect(remove).toHaveBeenCalledWith('research_chat');
  expect(select).not.toHaveBeenCalled();
});
