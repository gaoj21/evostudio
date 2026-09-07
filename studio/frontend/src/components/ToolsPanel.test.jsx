import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import ToolsPanel from './ToolsPanel.jsx';

const api = vi.hoisted(() => ({
  listCustomTools: vi.fn(),
  listSkills: vi.fn(),
  deleteCustomTool: vi.fn(),
  deleteSkill: vi.fn(),
  saveCustomTool: vi.fn(),
  saveSkill: vi.fn(),
}));

vi.mock('../api.js', () => ({ api }));

beforeEach(() => {
  vi.clearAllMocks();
  api.listCustomTools.mockResolvedValue({ tools: [] });
  api.listSkills.mockResolvedValue({ skills: [] });
});

describe('custom capabilities', () => {
  it('offers both a custom node and a custom tool', async () => {
    const onAdd = vi.fn();
    const user = userEvent.setup();
    render(<ToolsPanel onAdd={onAdd} />);

    await user.click(screen.getByRole('button', { name: '+ Custom node' }));
    expect(onAdd).toHaveBeenCalledWith(expect.objectContaining({
      type: 'custom',
      defaults: expect.objectContaining({ parse_mode: 'str' }),
    }));

    await user.click(screen.getByRole('button', { name: '+ Custom tool' }));
    expect(screen.getByRole('heading', { name: 'Custom toolkit' })).toBeInTheDocument();
    await waitFor(() => expect(api.listCustomTools).toHaveBeenCalled());
  });
});
