import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { describe, expect, it, vi } from 'vitest';

import TopBar from './TopBar.jsx';

const GRAPHS = [
  { id: 'credit-risk', name: 'Credit Risk' },
  { id: 'other', name: 'Other' },
];

function setup(props = {}) {
  const onRename = vi.fn();
  const onSelectGraph = vi.fn();
  render(
    <TopBar
      graphs={GRAPHS}
      graphId="credit-risk"
      onRename={onRename}
      onSelectGraph={onSelectGraph}
      {...props}
    />
  );
  return { onRename, onSelectGraph, user: userEvent.setup() };
}

const pencil = () => screen.getByRole('button', { name: 'Rename this workflow' });
const field = () => screen.getByRole('textbox', { name: 'Workflow name' });

describe('renaming the open workflow', () => {
  it('is offered where the name is shown', () => {
    // It used to live only in the Inspector, which shows workflow settings
    // when *nothing* is selected — so the moment you clicked a node while
    // building, there was nowhere left to name what you were building.
    setup();
    expect(pencil()).toBeInTheDocument();
  });

  it('edits the current name in place', async () => {
    const { user } = setup();
    await user.click(pencil());

    expect(field()).toHaveValue('Credit Risk');
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
  });

  it('commits on Enter', async () => {
    const { onRename, user } = setup();
    await user.click(pencil());
    await user.clear(field());
    await user.type(field(), 'Credit Watch{Enter}');

    expect(onRename).toHaveBeenCalledWith('Credit Watch');
    expect(screen.getByRole('combobox')).toBeInTheDocument();
  });

  it('commits when the field loses focus', async () => {
    // Clicking away is what people do; losing the edit there would be worse
    // than committing it.
    const { onRename, user } = setup();
    await user.click(pencil());
    await user.clear(field());
    await user.type(field(), 'Credit Watch');
    await user.tab();

    expect(onRename).toHaveBeenCalledWith('Credit Watch');
  });

  it('abandons the edit on Escape', async () => {
    const { onRename, user } = setup();
    await user.click(pencil());
    await user.clear(field());
    await user.type(field(), 'Regretted{Escape}');

    expect(onRename).not.toHaveBeenCalled();
    expect(screen.getByRole('combobox')).toBeInTheDocument();
  });

  it('does nothing when the name was not changed', async () => {
    const { onRename, user } = setup();
    await user.click(pencil());
    await user.type(field(), '{Enter}');

    expect(onRename).not.toHaveBeenCalled();
  });

  it('refuses to leave a workflow with no name', async () => {
    const { onRename, user } = setup();
    await user.click(pencil());
    await user.clear(field());
    await user.type(field(), '   {Enter}');

    expect(onRename).not.toHaveBeenCalled();
  });

  it('trims what was typed', async () => {
    const { onRename, user } = setup();
    await user.click(pencil());
    await user.clear(field());
    await user.type(field(), '  Credit Watch  {Enter}');

    expect(onRename).toHaveBeenCalledWith('Credit Watch');
  });

  it('is unavailable while a run is on the canvas', () => {
    setup({ runMode: true });
    expect(pencil()).toBeDisabled();
  });

  it('is unavailable when no workflow is open', () => {
    setup({ graphId: '' });
    expect(pencil()).toBeDisabled();
  });

  it('is offered on a narrow screen too', () => {
    // The bar collapses to identity plus Run and Save; the name is identity.
    setup({ compact: true });
    expect(pencil()).toBeInTheDocument();
  });

  it('still switches workflows', async () => {
    const { onSelectGraph, user } = setup();
    await user.selectOptions(screen.getByRole('combobox'), 'other');
    expect(onSelectGraph).toHaveBeenCalledWith('other');
  });
});
