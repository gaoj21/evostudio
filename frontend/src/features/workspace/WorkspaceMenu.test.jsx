import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
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
    workspaceDownloadUrl: (id, path) => `/api/graphs/${id}/workspace/download?path=${path}`,
  },
}));

const { api } = await import('../../api.js');

const FILES = [
  { path: 'workflow.py', size: 1200, mtime: '' },
  { path: 'runs', dir: true, size: 0, mtime: '' },
  { path: 'runs/r1/output.json', size: 300, mtime: '' },
  { path: 'memory/decide/001.json', size: 400, mtime: '' },
];

function setup(files = FILES) {
  api.getWorkspace.mockResolvedValue({ files });
  api.getWorkspaceFile.mockResolvedValue({ content: '# code', truncated: false });
  render(<WorkspacePanel open graphId="probe" onClose={vi.fn()} />);
  return userEvent.setup();
}

// A folder row starts with its chevron, so rows are matched by their aria
// label rather than by the text you see.
async function folderRow(name) {
  return waitFor(() => {
    const found = [...document.querySelectorAll('.ws-folder')]
      .find((el) => el.getAttribute('aria-label') === `${name} folder`);
    if (!found) throw new Error(`no folder named ${name}`);
    return found;
  });
}

async function openFolder(name) {
  const row = await folderRow(name);
  fireEvent.click(row);
  return row;
}

async function menuFor(name, isDir = false) {
  const row = isDir ? await folderRow(name) : await waitFor(() => {
    const found = [...document.querySelectorAll('.ws-file')]
      .find((el) => el.textContent.startsWith(name));
    if (!found) throw new Error(`no workspace file named ${name}`);
    return found;
  });
  fireEvent.contextMenu(row, { clientX: 40, clientY: 40 });
  return document.querySelector('.ctx-menu');
}

const item = (label) => screen.getByRole('button', { name: label });

beforeEach(() => {
  // Which folders are open is remembered per workflow, so it carries between
  // tests unless it is cleared.
  window.localStorage.clear();
  api.deleteWorkspaceFile.mockResolvedValue({ ok: true });
  vi.spyOn(window, 'confirm').mockReturnValue(true);
});

describe('workspace right-click menu', () => {
  it('offers what you can do with a file', async () => {
    setup();
    const menu = await menuFor('workflow.py');

    expect(menu).toBeTruthy();
    expect([...menu.querySelectorAll('.ctx-item')].map((b) => b.textContent))
      .toEqual(['Open', 'Download', 'Copy path', 'Delete']);
  });

  it('offers what you can do with a folder', async () => {
    setup();
    await openFolder('runs');
    const menu = await menuFor('r1', true);

    // A folder cannot be opened in the viewer, and downloads as a zip.
    expect([...menu.querySelectorAll('.ctx-item')].map((b) => b.textContent))
      .toEqual(['Download as zip', 'Copy path', 'Delete folder']);
  });

  it('names the thing it is about to act on', async () => {
    setup();
    await openFolder('runs');
    const menu = await menuFor('r1', true);
    expect(menu.querySelector('.ctx-path').textContent).toBe('runs/r1');
  });

  it('downloads through a link, so the file never passes through JS', async () => {
    setup();
    await menuFor('workflow.py');
    const clicked = [];
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function spy() {
      clicked.push(this.href);
    });

    await userEvent.setup().click(item('Download'));
    expect(clicked[0]).toContain('/workspace/download?path=workflow.py');
  });

  it('deletes a file', async () => {
    setup();
    await menuFor('workflow.py');
    await userEvent.setup().click(item('Delete'));

    await waitFor(() =>
      expect(api.deleteWorkspaceFile).toHaveBeenCalledWith('probe', 'workflow.py', false));
  });

  it('asks a second time before emptying a folder that holds things', async () => {
    // The server refuses the first attempt and says how much is inside; one
    // careless click on runs/ would otherwise take every run with it.
    api.deleteWorkspaceFile
      .mockRejectedValueOnce({ body: { detail: "'runs' holds 2 file(s). Deleting it removes them too." } })
      .mockResolvedValueOnce({ ok: true, files: 2 });
    setup();
    await openFolder('runs');
    await menuFor('r1', true);
    await userEvent.setup().click(item('Delete folder'));

    await waitFor(() => expect(api.deleteWorkspaceFile).toHaveBeenCalledTimes(2));
    expect(api.deleteWorkspaceFile.mock.calls[1]).toEqual(['probe', 'runs/r1', true]);
    expect(window.confirm).toHaveBeenCalledWith(
      expect.stringContaining('Delete it and everything in it?'));
  });

  it('keeps the folder when the second question is declined', async () => {
    api.deleteWorkspaceFile.mockRejectedValueOnce({
      body: { detail: "'runs' holds 2 file(s). Deleting it removes them too." },
    });
    window.confirm.mockReturnValueOnce(true).mockReturnValueOnce(false);
    setup();
    await openFolder('runs');
    await menuFor('r1', true);
    await userEvent.setup().click(item('Delete folder'));

    await waitFor(() => expect(api.deleteWorkspaceFile).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/removes them too/)).toBeInTheDocument();
  });

  it('copies the path', async () => {
    setup();
    await openFolder('runs');
    await menuFor('r1', true);
    // Defined after the menu is up: userEvent.setup() installs a clipboard
    // stub of its own, so a plain click is used here instead.
    const writeText = vi.fn().mockResolvedValue();
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    fireEvent.click(item('Copy path'));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith('runs/r1'));
  });

  it('closes on Escape without doing anything', async () => {
    setup();
    await menuFor('workflow.py');

    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => expect(document.querySelector('.ctx-menu')).toBeNull());
    expect(api.deleteWorkspaceFile).not.toHaveBeenCalled();
  });

  it('closes when you click away', async () => {
    setup();
    await menuFor('workflow.py');

    fireEvent.mouseDown(document.body);
    await waitFor(() => expect(document.querySelector('.ctx-menu')).toBeNull());
  });

  it('does not offer to delete a memory entry', async () => {
    // Memory is a view of a vector store; the server refuses to delete from
    // it, so offering the option would only lead to a refusal.
    setup();
    await openFolder('memory');
    await openFolder('decide');
    const menu = await menuFor('001.json');

    expect([...menu.querySelectorAll('.ctx-item')].map((b) => b.textContent))
      .toEqual(['Open', 'Download', 'Copy path']);
  });

  it('does not offer to delete a memory folder either', async () => {
    setup();
    await openFolder('memory');
    const menu = await menuFor('decide', true);

    expect([...menu.querySelectorAll('.ctx-item')].map((b) => b.textContent))
      .toEqual(['Download as zip', 'Copy path']);
  });

  it('still downloads memory', async () => {
    setup();
    await openFolder('memory');
    const menu = await menuFor('decide', true);
    const clicked = [];
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function spy() {
      clicked.push(this.href);
    });

    fireEvent.click([...menu.querySelectorAll('.ctx-item')]
      .find((b) => b.textContent === 'Download as zip'));
    expect(clicked[0]).toContain('memory/decide');
  });
});
