import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
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

it('copies the usable full path for uploaded dataset folders and files',async()=>{
  setup([{path:'datasets',dir:true,readonly:true},
    {path:'datasets/upload',dir:true,readonly:true,absolute_path:'/server/data/files'},
    {path:'datasets/upload/a.json',size:2,mtime:'',readonly:true,absolute_path:'/server/data/files/a.json'}]);
  const clipboard=vi.spyOn(navigator.clipboard,'writeText').mockResolvedValue();
  await openFolder('datasets');
  await menuFor('upload',true);
  fireEvent.click(item('Copy path'));
  expect(clipboard).toHaveBeenLastCalledWith('/server/data/files');
  await openFolder('upload');
  await menuFor('a.json');
  expect(screen.queryByRole('button',{name:'Delete',exact:true})).not.toBeInTheDocument();
  fireEvent.click(item('Copy path'));
  expect(clipboard).toHaveBeenLastCalledWith('/server/data/files/a.json');
});


describe('creating a file', () => {
  it('suggests a free path, refuses an existing one, and opens the editor on a new file', async () => {
    api.getWorkspace.mockResolvedValue({ files: [{ path: 'files/notes.txt', size: 5, mtime: '' }] });
    api.getWorkspaceFile.mockImplementation(async (_g, path) => ({ path, content: '' }));
    api.saveWorkspaceFile.mockResolvedValue({});
    const user = userEvent.setup();
    render(<WorkspacePanel open graphId="g" onClose={() => {}} />);
    await waitFor(() => expect(api.getWorkspace).toHaveBeenCalled());
    await act(async () => {});
    await user.click(screen.getByText('+ New file'));
    const path = screen.getByPlaceholderText('files/notes.txt');
    expect(path).toHaveValue('files/notes-2.txt');
    await user.clear(path);
    await user.type(path, 'files/notes.txt');
    await user.click(screen.getByText('Create'));
    expect(await screen.findByRole('alert')).toHaveTextContent('already exists');
    expect(api.saveWorkspaceFile).not.toHaveBeenCalled();
    await user.clear(path);
    await user.type(path, 'files/new.txt');
    await user.click(screen.getByText('Create'));
    expect(api.saveWorkspaceFile).toHaveBeenCalledWith('g', 'files/new.txt', '');
    await waitFor(() => expect(document.querySelector('textarea.ws-editor')).not.toBeNull());
  });
});

it('uploads a folder preserving nested paths under the chosen destination', async()=>{
  const user=setup();
  api.uploadWorkspaceFile.mockResolvedValue({});
  await user.clear(screen.getByLabelText('Upload / create in'));
  await user.type(screen.getByLabelText('Upload / create in'),'files/checkpoints');
  const weights=new File(['weights'],'model.bin');
  Object.defineProperty(weights,'webkitRelativePath',{value:'model/nested/model.bin'});
  const config=new File(['{}'],'config.json');
  Object.defineProperty(config,'webkitRelativePath',{value:'model/config.json'});
  await user.upload(screen.getByLabelText('Upload folder'),[weights,config]);
  await waitFor(()=>expect(api.uploadWorkspaceFile).toHaveBeenCalledWith('probe',weights,'files/checkpoints/model/nested/model.bin'));
  expect(api.uploadWorkspaceFile).toHaveBeenCalledWith('probe',config,'files/checkpoints/model/config.json');
  expect(await screen.findByRole('status')).toHaveTextContent('Uploaded 2 file(s)');
});

it('reports partial uploads without hiding successful files', async()=>{
  const user=setup();
  api.uploadWorkspaceFile.mockResolvedValueOnce({}).mockRejectedValueOnce({body:{detail:'already exists'}});
  await user.upload(screen.getByLabelText('Upload files'),[new File(['a'],'a.bin'),new File(['b'],'b.bin')]);
  expect(await screen.findByText(/1\/2 files uploaded.*already exists/)).toBeVisible();
  expect(screen.getByRole('button',{name:'Upload files'})).toBeEnabled();
});

it('shows checkpoint metadata and copy path without a text editor', async()=>{
  const user=setup([{path:'model.pt',size:8,absolute_path:'/server/workspace/model.pt'}]);
  api.getWorkspaceFile.mockResolvedValue({path:'model.pt',binary:true,size:8,content:''});
  await user.click(await screen.findByText('model.pt'));
  expect(await screen.findByText(/Binary file/)).toBeVisible();
  expect(screen.queryByRole('button',{name:'Edit',exact:true})).not.toBeInTheDocument();
  const clipboard=vi.spyOn(navigator.clipboard,'writeText').mockResolvedValue();
  await user.click(screen.getByRole('button',{name:'Copy full path'}));
  expect(clipboard).toHaveBeenCalledWith('/server/workspace/model.pt');
});
