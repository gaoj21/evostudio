import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import Palette from './Palette.jsx';

const api = vi.hoisted(() => ({ listTools: vi.fn() }));
vi.mock('../../api.js', () => ({ api }));

const templates = [
  { type: 'agent', label: 'LLM Task', description: 'General purpose task' },
  { type: 'cr-detect', label: 'CR Detect', description: 'Credit risk detection' },
];
const sources = [
  { type: 'gdelt', label: 'GDELT News', description: 'Recent news source' },
];

beforeEach(() => {
  vi.clearAllMocks();
  api.listTools.mockResolvedValue({
    tools: [{
      name: 'StorageToolkit',
      available: true,
      tools: [{ name: 'save', description: 'Save a file', inputs: [] }],
    }],
  });
});

function setup() {
  render(
    <Palette
      templates={templates}
      sources={sources}
      graphTemplates={[{ id: 'monitor', name: 'Risk Monitor', description: 'Ready workflow' }]}
      onAdd={vi.fn()}
      onLoadTemplate={vi.fn()}
    />
  );
  return userEvent.setup();
}

describe('node library hierarchy', () => {
  it('shows core nodes first and keeps specialist groups folded', async () => {
    setup();

    expect(screen.getByText('LLM Task')).toBeInTheDocument();
    expect(screen.queryByText('CR Detect')).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: /Tools/ })).toBeInTheDocument());
    expect(screen.queryByText('⚙ save')).not.toBeInTheDocument();
  });

  it('opens a specialist group only when requested', async () => {
    const user = setup();
    await user.click(screen.getByRole('button', { name: /Credit risk/ }));
    expect(screen.getByText('CR Detect')).toBeInTheDocument();
  });

  it('searches across folded groups and exposes matching results', async () => {
    const user = setup();
    await waitFor(() => expect(api.listTools).toHaveBeenCalled());
    await user.type(screen.getByRole('searchbox', { name: 'Search node library' }), 'save');

    expect(await screen.findByText('save')).toBeInTheDocument();
    expect(screen.queryByText('LLM Task')).not.toBeInTheDocument();
  });
});
