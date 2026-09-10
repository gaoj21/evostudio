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
  uploadCustomTool: vi.fn(),
  installCustomToolRequirements: vi.fn(),
  saveSkill: vi.fn(),
}));

vi.mock('../../api.js', () => ({ api }));

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

describe('a custom tool can be an input source', () => {
  it('marking a function re-saves the toolkit with it as a source', async () => {
    api.listCustomTools.mockResolvedValue({ tools: [{
      name: 'feedkit', description: 'feeds', code: 'def tickers(limit: int = 2) -> list: ...',
      tools: [{ name: 'tickers', description: 't', params: [] }], sources: [] }] });
    api.saveCustomTool.mockResolvedValue({});
    render(<ToolsPanel onAdd={vi.fn()} />);
    const box = await screen.findByLabelText('tickers as input source');
    expect(box).not.toBeChecked();
    await userEvent.setup().click(box);

    await waitFor(() => expect(api.saveCustomTool).toHaveBeenCalledWith(
      { name: 'feedkit', code: 'def tickers(limit: int = 2) -> list: ...', sources: ['tickers'] }));
  });
});


describe('a toolkit can be uploaded as a folder', () => {
  it('sends the zip with the name and entry given, and lists what it found', async () => {
    api.uploadCustomTool.mockResolvedValue({ name: 'my_project',
      tools: [{ name: 'clean' }], package: { entry: 'src/api.py', files: 4, requirements: ['requests>=2'] } });
    render(<ToolsPanel onAdd={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: /upload folder/i }));

    const zip = new File(['zip bytes'], 'proj.zip', { type: 'application/zip' });
    await user.upload(screen.getByLabelText(/folder \(\.zip\)/i), zip);
    await user.type(screen.getByLabelText(/toolkit name/i), 'my_project');
    await user.type(screen.getByLabelText(/entry file/i), 'src/api.py');
    await user.click(screen.getByRole('button', { name: /^upload$/i }));

    await waitFor(() => expect(api.uploadCustomTool).toHaveBeenCalledWith(
      zip, { name: 'my_project', entry: 'src/api.py' }));
    expect(await screen.findByText(/needs requests>=2/)).toBeInTheDocument();
  });

  it('installs a folder toolkit\'s requirements only when asked', async () => {
    api.listCustomTools.mockResolvedValue({ tools: [{
      name: 'my_project', description: 'p', code: '', tools: [{ name: 'clean', params: [] }],
      sources: [], package: { entry: 'tools.py', files: 3, requirements: ['requests>=2'] } }] });
    api.installCustomToolRequirements.mockResolvedValue({ installed: ['requests>=2'], output: '' });
    render(<ToolsPanel onAdd={vi.fn()} />);
    const button = await screen.findByRole('button', { name: /install requirements/i });
    expect(api.installCustomToolRequirements).not.toHaveBeenCalled();
    await userEvent.setup().click(button);

    await waitFor(() => expect(api.installCustomToolRequirements).toHaveBeenCalledWith('my_project'));
    expect(await screen.findByText(/Installed for my_project/)).toBeInTheDocument();
  });
});
