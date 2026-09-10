import React, { useEffect, useState } from 'react';
import App from './App.jsx';
import ResourceActions from './components/ResourceActions.jsx';
import ResourceMenu from './components/ResourceMenu.jsx';
import TaskDetail from './TaskDetail.jsx';
import { api } from './api.js';
import './platform.css';

function errorText(error) {
  const detail = error?.body?.detail;
  return typeof detail === 'string' ? detail : error.message || 'Please try again.';
}

export default function Platform() {
  const [projects, setProjects] = useState([]);
  const [selected, setSelected] = useState('unassigned');
  const [tasks, setTasks] = useState([]);
  const [editor, setEditor] = useState(null);
  const [detail, setDetail] = useState(null);
  const [prepareRun, setPrepareRun] = useState(false);
  const [form, setForm] = useState(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [menu, setMenu] = useState(null);
  const [taskAction, setTaskAction] = useState(null);
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    let active = true;
    api.listProjects().then(rows => { if (active) setProjects(rows); })
      .catch(e => { if (active) setError(errorText(e)); });
    return () => { active = false; };
  }, [revision]);
  useEffect(() => {
    let active = true;
    setLoading(true); setTasks([]); setError('');
    api.projectTasks(selected).then(rows => { if (active) setTasks(rows); })
      .catch(e => { if (active) setError(errorText(e)); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [selected, revision]);

  const project = projects.find(p => p.id === selected);
  async function submit(event) {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(event.currentTarget));
    setBusy(true); setError('');
    try {
      if (form === 'project') {
        const created = await api.createProject(values);
        setProjects(previous => [created, ...previous]); setSelected(created.id);
      } else {
        const task = await api.createTask(selected, values);
        setDetail(task.id);
      }
      setForm(null); setRevision(v => v + 1);
    } catch (e) { setError(errorText(e)); }
    finally { setBusy(false); }
  }
  async function assign(task, projectId) {
    if (!projectId) return;
    setBusy(true); setError('');
    try { await api.assignTask(projectId, task.id); setRevision(v => v + 1); }
    catch (e) { setError(errorText(e)); }
    finally { setBusy(false); }
  }

  function openMenu(event, task) {
    event.preventDefault();
    const rect = event.currentTarget.getBoundingClientRect();
    setMenu({ task, x: event.type === 'contextmenu' ? event.clientX : rect.left, y: event.type === 'contextmenu' ? event.clientY : rect.bottom, trigger: event.currentTarget });
  }
  async function applyTaskAction(event) {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(event.currentTarget));
    setBusy(true); setError('');
    try {
      if (taskAction.kind === 'delete') await api.deleteGraph(taskAction.task.id);
      if (taskAction.kind === 'rename-project') await api.updateProject(taskAction.task.id, { name: values.name.trim(), description: taskAction.task.description || '' });
      if (taskAction.kind === 'rename') await api.renameGraph(taskAction.task.id, values.name.trim());
      if (taskAction.kind === 'move') await api.assignTask(values.project, taskAction.task.id);
      if (taskAction.kind === 'delete-project') { await api.deleteProject(taskAction.task.id); setSelected('unassigned'); }
      setTaskAction(null); setRevision(v => v + 1);
    } catch (e) { setError(errorText(e)); }
    finally { setBusy(false); }
  }

  function startTask(projectId = selected) {
    setSelected(projectId); setForm('task'); setTaskAction(null); setError('');
  }
  function projectActions(p) {
    return [
      { label: 'Open project', action: () => { setSelected(p.id); setForm(null); } },
      { label: 'New task', disabled: busy, action: () => startTask(p.id) },
      { label: 'Rename project', disabled: busy, action: () => { setError(''); setTaskAction({ kind: 'rename-project', task: p }); } },
      { label: 'Delete project…', danger: true, disabled: busy, action: () => { setError(''); setTaskAction({ kind: 'delete-project', task: p }); } },
    ];
  }

  if (editor) return <App key={editor} initialGraphId={editor} projectId={selected} initialRun={prepareRun} onHome={() => { setEditor(null); setPrepareRun(false); setDetail(null); setRevision(v => v + 1); }} />;
  if (detail) return <TaskDetail key={detail} graphId={detail} onBack={() => { setDetail(null); setRevision(v => v + 1); }} onEdit={() => { setPrepareRun(false); setEditor(detail); }} onRun={() => { setPrepareRun(true); setEditor(detail); }} />;
  return <div className="platform">
    <aside className="project-sidebar">
      <div className="platform-brand"><span className="brand-mark">E</span> EvoAgentX <small>WORKSPACE</small></div>
      <div className="project-nav-heading">PROJECTS <button aria-label="Create project" onClick={() => { setForm('project'); setError(''); }}>+</button></div>
      <nav aria-label="Projects">
        {projects.map(p => <ResourceActions key={p.id} name={p.name} items={projectActions(p)}><button className={selected === p.id ? 'selected' : ''} onClick={() => { setSelected(p.id); setForm(null); }}><span>▧</span>{p.name}</button></ResourceActions>)}
        <button className={selected === 'unassigned' ? 'selected' : ''} onClick={() => { setSelected('unassigned'); setForm(null); }}><span>▤</span>Unassigned tasks</button>
      </nav>
      <p className="sidebar-note">One place for your goals,<br />agents, and repeatable work.</p>
    </aside>
    <main className="project-main">
      <header className="project-heading"><div><div className="project-eyebrow">YOUR WORKSPACE / {project ? 'PROJECT' : 'EXISTING WORK'}</div>{project ? <ResourceActions name={project.name} items={projectActions(project)}><h1>{project.name}</h1></ResourceActions> : <h1>Unassigned tasks</h1>}<p>{project?.description || 'Your existing workflows are here. Organize them into projects when you are ready.'}</p></div>
        <div className="form-actions"><button disabled={busy} onClick={() => { setForm('project'); setError(''); }}>+ New project</button><button className="platform-primary" disabled={busy} onClick={() => startTask()}>+ New task</button></div>
      </header>
      {error && <div className="platform-error" role="alert">{error} <button onClick={() => setRevision(v => v + 1)}>Retry loading</button></div>}
      {form && <section className="project-form" aria-label={form === 'project' ? 'New project' : 'New task'}>
        <h2>{form === 'project' ? 'Give your work a home' : 'What should the agent do?'}</h2>
        <p>{form === 'project' ? 'Group related tasks under a project. Any domain, any goal.' : 'Start with a goal. You can add tools, skills, and steps in the editor.'}</p>
        <form onSubmit={submit}>
          <label>Name<input name="name" required maxLength={120} autoFocus placeholder={form === 'project' ? 'e.g. Customer research' : 'e.g. Summarize customer feedback'} /></label>
          {form === 'project' ? <label>Description<textarea name="description" maxLength={4000} placeholder="What is this project for?" /></label> : <>
            <label>Goal<textarea name="goal" required maxLength={12000} placeholder="Describe the work and what a good result looks like." /></label>
            <label>Expected output<textarea name="output_description" maxLength={4000} defaultValue="A clear answer with supporting evidence." /></label>
            <p className="form-note">Starts with one agent and a text input. Creating a task does not call a model. Review the model and configuration before running.</p>
          </>}
          <div className="form-actions"><button type="button" disabled={busy} onClick={() => setForm(null)}>Cancel</button><button className="platform-primary" disabled={busy}>{busy ? 'Creating…' : form === 'project' ? 'Create project' : 'Create task'}</button></div>
        </form>
      </section>}
      <section className="project-task-section" aria-label="Tasks"><div className="task-section-heading"><h2>Tasks</h2><span>{loading ? 'Loading…' : `${tasks.length} total`}</span></div>
        {!loading && !tasks.length && <div className="project-empty"><h3>Start with one useful task</h3><p>Describe a goal, provide your input, and build from there.</p><button className="platform-primary" disabled={busy} onClick={() => startTask()}>Create your first task</button></div>}
        <div className="project-task-grid">{tasks.map(task => <article className="project-task-card" key={task.id} onContextMenu={e => openMenu(e, task)}>
          <div className="task-card-heading"><div className="task-kind">AGENT TASK</div><button className="task-more" aria-label={`Actions for ${task.name}`} aria-haspopup="menu" disabled={busy} onClick={e => openMenu(e, task)}>⋯</button></div><h3>{task.name}</h3><p>{task.goal || 'Open this task to define its goal and execution steps.'}</p>
          <div className="task-card-actions"><button onClick={() => setDetail(task.id)}>Open task →</button>{!project && projects.length > 0 && <select value="" aria-label={`Move ${task.name} to project`} disabled={busy} onChange={e => assign(task, e.target.value)}><option value="">Move to project…</option>{projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select>}</div>
        </article>)}</div>
      </section>
    </main>
    {menu && <ResourceMenu menu={menu} onClose={() => setMenu(null)} items={[
      { label: 'Open task', action: () => setDetail(menu.task.id) },
      { label: 'Edit workflow', action: () => { setPrepareRun(false); setEditor(menu.task.id); } },
      ...['rename', 'move', 'delete'].map(kind => ({ label: { rename: 'Rename', move: 'Move to project…', delete: 'Delete task…' }[kind], danger: kind === 'delete', disabled: busy, action: () => { setError(''); setTaskAction({ kind, task: menu.task }); } })),
    ]} />}
    {taskAction && <div className="resource-dialog-backdrop"><section role="dialog" aria-modal="true" aria-label="Task action" className="project-form resource-dialog" onKeyDown={e => { if (e.key === 'Escape' && !busy) setTaskAction(null); }}>
      <h2>{{ 'rename-project': 'Rename project', rename: 'Rename task', move: 'Move task', delete: 'Delete task?', 'delete-project': 'Delete project?' }[taskAction.kind]}</h2>
      <p>{taskAction.task.name}</p>
      {error && <p role="alert" className="platform-error">{error}</p>}
      <form onSubmit={applyTaskAction}>
        {['rename', 'rename-project'].includes(taskAction.kind) && <label>Name<input autoFocus name="name" required maxLength={120} defaultValue={taskAction.task.name} /></label>}
        {taskAction.kind === 'move' && <label>Project<select autoFocus name="project" defaultValue={selected}><option value="unassigned">Unassigned tasks</option>{projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>}
        {taskAction.kind === 'delete' && <p>Remove this task and its workflow definition. Stored run results and Memory data are retained.</p>}
        {taskAction.kind === 'delete-project' && <p>Only empty projects can be deleted. Move or delete their tasks first.</p>}
        <div className="form-actions"><button autoFocus={taskAction.kind.startsWith('delete')} type="button" disabled={busy} onClick={() => setTaskAction(null)}>Cancel</button><button disabled={busy} className={taskAction.kind.startsWith('delete') ? 'danger' : 'platform-primary'}>{busy ? 'Saving…' : taskAction.kind.startsWith('delete') ? 'Delete' : 'Save'}</button></div>
      </form>
    </section></div>}
  </div>;
}
