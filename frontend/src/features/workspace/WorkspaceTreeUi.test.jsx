import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import WorkspacePanel from './WorkspacePanel.jsx';

vi.mock('../../api.js', () => ({
  api: {
    getWorkspace: vi.fn(),
    getWorkspaceFile: vi.fn(),
    saveWorkspaceFile: vi.fn(),
    uploadWorkspaceFile: vi.fn(),
    deleteWorkspaceFile: vi.fn(),
    mkdirWorkspace: vi.fn(),
    workspaceDownloadUrl: (id, path) => `/d?${path}`,
  },
}));

const { api } = await import('../../api.js');

const FILES = [
  { path: 'workflow.py', size: 100, mtime: '' },
  { path: 'runs/r1/output.json', size: 50, mtime: '' },
  { path: 'runs/r1/nodes/01-first.json', size: 20, mtime: '' },
  { path: 'runs/r2/output.json', size: 50, mtime: '' },
];

function setup(graphId = 'probe', files = FILES) {
  api.getWorkspace.mockResolvedValue({ files });
  api.getWorkspaceFile.mockResolvedValue({ content: '# code', truncated: false });
  return render(<WorkspacePanel open graphId={graphId} onClose={vi.fn()} />);
}

const folder = (name) => screen.getByRole('button', { name: `${name} folder` });
const visibleFiles = () =>
  [...document.querySelectorAll('.ws-file')].map((e) => e.textContent.replace(/✕$/, '').trim());

beforeEach(() => {
  window.localStorage.clear();
});

describe('folding the workspace tree', () => {
  it('starts folded, showing only the top level', async () => {
    setup();
    await waitFor(() => expect(folder('runs')).toBeInTheDocument());

    // Eight nodes across three runs is a lot to unroll at you unasked.
    expect(visibleFiles()).toEqual(['workflow.py 100B']);
    expect(folder('runs')).toHaveAttribute('aria-expanded', 'false');
  });

  it('says how much a folded folder is hiding', async () => {
    setup();
    await waitFor(() => expect(folder('runs')).toBeInTheDocument());
    expect(folder('runs').querySelector('.ws-count').textContent).toBe('3');
  });

  it('unfolds one level at a time', async () => {
    setup();
    await waitFor(() => expect(folder('runs')).toBeInTheDocument());

    fireEvent.click(folder('runs'));
    expect(folder('r1')).toBeInTheDocument();
    expect(visibleFiles()).toEqual(['workflow.py 100B']);   // r1 is still folded

    fireEvent.click(folder('r1'));
    expect(visibleFiles()).toContain('output.json 50B');
    expect(folder('nodes')).toBeInTheDocument();
  });

  it('folds again', async () => {
    setup();
    await waitFor(() => expect(folder('runs')).toBeInTheDocument());
    fireEvent.click(folder('runs'));
    expect(screen.queryByRole('button', { name: 'r1 folder' })).toBeInTheDocument();

    fireEvent.click(folder('runs'));
    expect(screen.queryByRole('button', { name: 'r1 folder' })).not.toBeInTheDocument();
  });

  it('drops the count once a folder is open', async () => {
    setup();
    await waitFor(() => expect(folder('runs')).toBeInTheDocument());
    fireEvent.click(folder('runs'));
    // What is inside is on screen; saying "3" as well is noise.
    expect(folder('runs').querySelector('.ws-count')).toBeNull();
  });

  it('remembers what was open, per workflow', async () => {
    const { unmount } = setup();
    await waitFor(() => expect(folder('runs')).toBeInTheDocument());
    fireEvent.click(folder('runs'));
    unmount();

    setup();
    await waitFor(() => expect(folder('runs')).toBeInTheDocument());
    expect(folder('runs')).toHaveAttribute('aria-expanded', 'true');
  });

  it('does not carry one workflow\'s shape onto another', async () => {
    const { unmount } = setup('probe');
    await waitFor(() => expect(folder('runs')).toBeInTheDocument());
    fireEvent.click(folder('runs'));
    unmount();

    setup('other');
    await waitFor(() => expect(folder('runs')).toBeInTheDocument());
    expect(folder('runs')).toHaveAttribute('aria-expanded', 'false');
  });

  it('indents by depth, so the nesting is readable', async () => {
    setup();
    await waitFor(() => expect(folder('runs')).toBeInTheDocument());
    fireEvent.click(folder('runs'));
    fireEvent.click(folder('r1'));

    const depth = (el) => parseInt(el.style.paddingLeft, 10);
    expect(depth(folder('runs'))).toBeLessThan(depth(folder('r1')));
    expect(depth(folder('r1'))).toBeLessThan(depth(folder('nodes')));
  });

  it('opens a file from inside a folder', async () => {
    setup();
    await waitFor(() => expect(folder('runs')).toBeInTheDocument());
    fireEvent.click(folder('runs'));
    fireEvent.click(folder('r1'));
    fireEvent.click([...document.querySelectorAll('.ws-file')]
      .find((e) => e.textContent.startsWith('output.json')));

    await waitFor(() =>
      expect(api.getWorkspaceFile).toHaveBeenCalledWith('probe', 'runs/r1/output.json'));
  });
});
