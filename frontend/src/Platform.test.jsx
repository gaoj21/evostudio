import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import Platform from './Platform.jsx';
import { api } from './api.js';
vi.mock('./api.js', () => ({api:{listProjects:vi.fn(),projectTasks:vi.fn(),createProject:vi.fn(),createTask:vi.fn(),copyTask:vi.fn(),assignTask:vi.fn(),deleteGraph:vi.fn(),renameGraph:vi.fn(),deleteProject:vi.fn()}}));
vi.mock('./TaskDetail.jsx', () => ({default:({graphId,onEdit,onRun}) => <div>Detail: {graphId}<button onClick={onEdit}>Edit workflow</button><button onClick={onRun}>Prepare run</button></div>}));
vi.mock('./App.jsx', () => ({default:({initialGraphId,onHome}) => <div>Editor: {initialGraphId}<button onClick={onHome}>Home</button></div>}));
beforeEach(() => { vi.resetAllMocks(); api.listProjects.mockResolvedValue([]); api.projectTasks.mockResolvedValue([]); });
it('keeps legacy workflows accessible without a domain default',async()=>{
 api.projectTasks.mockResolvedValue([{id:'old',name:'Existing task',goal:'Useful work'}]);
 render(<Platform/>);
 fireEvent.click(await screen.findByText('Open task →'));
 expect(screen.getByText('Detail: old')).toBeInTheDocument();
 fireEvent.click(screen.getByText('Edit workflow'));
 expect(screen.getByText('Editor: old')).toBeInTheDocument();
});
it('creates a generic project then an executable task',async()=>{
 api.createProject.mockResolvedValue({id:'p',name:'Research'});
 api.createTask.mockResolvedValue({id:'task'});
 render(<Platform/>);
 fireEvent.click(await screen.findByText('+ New project'));
 fireEvent.change(screen.getByLabelText('Name'),{target:{value:'Research'}});
 api.listProjects.mockResolvedValue([{id:'p',name:'Research'}]);
 fireEvent.click(screen.getByText('Create project'));
 fireEvent.click(await screen.findByText('+ New task'));
 fireEvent.change(screen.getByLabelText('Name'),{target:{value:'Summary'}});
 fireEvent.change(screen.getByLabelText('Goal'),{target:{value:'Summarize feedback'}});
 fireEvent.click(screen.getByText('Create task'));
 await waitFor(()=>expect(api.createTask).toHaveBeenCalledWith('p',expect.objectContaining({goal:'Summarize feedback'})));
 expect(await screen.findByText('Detail: task')).toBeInTheDocument();
});
it('shows a server failure instead of pretending creation succeeded',async()=>{
 api.createProject.mockRejectedValue(new Error('Storage unavailable'));
 render(<Platform/>);fireEvent.click(await screen.findByText('+ New project'));
 fireEvent.change(screen.getByLabelText('Name'),{target:{value:'Research'}});
 fireEvent.click(screen.getByText('Create project'));
 expect(await screen.findByRole('alert')).toHaveTextContent('Storage unavailable');
});

it('offers task actions by right click and deletes only after confirmation', async () => {
 api.projectTasks.mockResolvedValue([{ id: 'old', name: 'Existing task' }]);
 api.deleteGraph.mockResolvedValue({ ok: true });
 render(<Platform />);
 fireEvent.contextMenu((await screen.findByText('Existing task')).closest('article'), { clientX: 10, clientY: 20 });
 fireEvent.click(screen.getByRole('menuitem', { name: 'Delete task…' }));
 expect(api.deleteGraph).not.toHaveBeenCalled();
 api.projectTasks.mockResolvedValue([]);
 fireEvent.click(screen.getByRole('button', { name: 'Delete', exact: true }));
 await waitFor(() => expect(api.deleteGraph).toHaveBeenCalledWith('old'));
 await waitFor(() => expect(screen.queryByText('Existing task')).not.toBeInTheDocument());
});
it('opens the same task menu from the visible actions button', async () => {
 api.projectTasks.mockResolvedValue([{ id: 'old', name: 'Existing task' }]);
 render(<Platform />);
 fireEvent.click(await screen.findByLabelText('Actions for Existing task'));
 expect(screen.getByRole('menuitem', { name: 'Rename' })).toBeInTheDocument();
 fireEvent.keyDown(document, { key: 'Escape' });
 expect(screen.queryByRole('menu')).not.toBeInTheDocument();
});

it('creates an unassigned task directly without creating a project first', async () => {
 api.createTask.mockResolvedValue({ id: 'standalone' });
 render(<Platform />);
 fireEvent.click(await screen.findByRole('button', { name: '+ New task' }));
 fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Standalone' } });
 fireEvent.change(screen.getByLabelText('Goal'), { target: { value: 'Summarize feedback' } });
 fireEvent.click(screen.getByRole('button', { name: 'Create task', exact: true }));
 await waitFor(() => expect(api.createTask).toHaveBeenCalledWith('unassigned', expect.objectContaining({ name: 'Standalone' })));
 expect(await screen.findByText('Detail: standalone')).toBeInTheDocument();
});
it('creates in the right-clicked project and exposes management on the main project heading', async () => {
 api.listProjects.mockResolvedValue([{ id: 'research', name: 'Research' }]);
 api.createTask.mockResolvedValue({ id: 'new-task' });
 render(<Platform />);
 fireEvent.contextMenu(await screen.findByText('Research', { exact: true }));
 fireEvent.click(screen.getByRole('menuitem', { name: 'New task' }));
 fireEvent.contextMenu(screen.getByRole('heading', { name: 'Research', level: 1 }));
 expect(screen.getByRole('menuitem', { name: 'Delete project…' })).toBeInTheDocument();
 fireEvent.keyDown(document, { key: 'Escape' });
 fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Summary' } });
 fireEvent.change(screen.getByLabelText('Goal'), { target: { value: 'Summarize' } });
 fireEvent.click(screen.getByRole('button', { name: 'Create task', exact: true }));
 await waitFor(() => expect(api.createTask).toHaveBeenCalledWith('research', expect.objectContaining({ name: 'Summary' })));
});

it('copies a task into its current project and refreshes the task list',async()=>{
 api.projectTasks.mockResolvedValue([{id:'original',name:'Original'}]);
 api.copyTask.mockResolvedValue({id:'copy',name:'Original (copy)'});
 render(<Platform/>);
 fireEvent.click(await screen.findByLabelText('Actions for Original'));
 fireEvent.click(screen.getByRole('menuitem',{name:'Copy task'}));
 expect(api.copyTask).not.toHaveBeenCalled();
 api.projectTasks.mockResolvedValue([{id:'original',name:'Original'},{id:'copy',name:'Original (copy)'}]);
 fireEvent.click(screen.getByRole('button',{name:'Copy',exact:true}));
 await waitFor(()=>expect(api.copyTask).toHaveBeenCalledWith('unassigned','original'));
 expect(await screen.findByText('Original (copy)')).toBeVisible();
});
