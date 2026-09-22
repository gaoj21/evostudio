import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import ResourceActions from './ResourceActions.jsx';
it('offers the same actions from right-click and the visible menu without activating the card', () => {
  const add = vi.fn(), remove = vi.fn();
  render(<ResourceActions name="Resource" items={[{ label: 'Remove', action: remove }]}><button onClick={add}>Resource</button></ResourceActions>);
  fireEvent.contextMenu(screen.getByText('Resource'));
  fireEvent.click(screen.getByRole('menuitem', { name: 'Remove' }));
  expect(remove).toHaveBeenCalledTimes(1);
  expect(add).not.toHaveBeenCalled();
  fireEvent.click(screen.getByLabelText('Actions for Resource'));
  expect(screen.getByRole('menu')).toBeInTheDocument();
  fireEvent.keyDown(document, { key: 'Escape' });
  expect(screen.queryByRole('menu')).not.toBeInTheDocument();
});
