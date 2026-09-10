import React, { useEffect, useRef, useState } from 'react';
import { memoryId, memoryBinding } from './useCanvasAgents.js';
import ResourceActions from '../../components/ResourceActions.jsx';
import { api } from '../../api.js';
const errorText = e => String(e.body?.detail || e.message);
export default function ChatAgentInspector({ graphId, agent, onBack, onSave, saving = false, memoryNodes = [], backLabel = '← Back to workflow' }) {
  const [page, setPage] = useState('chat');
  const [draft, setDraft] = useState(agent);
  const [baseline, setBaseline] = useState(agent);
  const [toolkits, setToolkits] = useState([]);
  const [skills, setSkills] = useState([]);
  const [capabilitiesLoading, setCapabilitiesLoading] = useState(true);
  const [toolSearch, setToolSearch] = useState('');
  const [spaces, setSpaces] = useState([]);
  const [sessions, setSessions] = useState([]);
  const [sessionId, setSessionId] = useState(null);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const deletedSessions = useRef(new Set());
  const [historyOpen, setHistoryOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [busy, setBusy] = useState(false);
  const session = sessions.find(s => s.id === sessionId);
  const running = ['running', 'stopping'].includes(session?.status);
  useEffect(() => {
    let active = true;
    Promise.all([api.listTools(), api.listSkills()]).then(([t, s]) => {
      if (active) { setToolkits(t.tools || []); setSkills(s.skills || []); }
    }).catch(e => { if (active) setError(errorText(e)); }).finally(() => { if (active) setCapabilitiesLoading(false); });
    return () => { active = false; };
  }, [graphId, agent.id]);
  useEffect(() => {
    setDraft(previous => ({ ...previous, memories: agent.memories }));
  }, [agent]);
  useEffect(() => {
    let active = true;
    let timer;
    async function refresh() {
      try {
        const r = await api.agentSessions(graphId, agent.id);
        if (!active) return;
        setSessions(r.sessions.filter(s => !deletedSessions.current.has(s.id)));
        setSessionId(id => id || r.sessions.find(s => !deletedSessions.current.has(s.id))?.id || null);
      } catch (e) { if (active) setError(errorText(e)); }
      if (active) timer = setTimeout(refresh, 1500);
    }
    refresh();
    (api.chatMemoryResources ? api.chatMemoryResources(graphId) : api.mem0Spaces(graphId).then(r => ({ resources: r.spaces.map(s => ({ ...s, id: `mem:space:${s.id}`, writable: true })) }))).then(r => { if (active) setSpaces(r.resources); }).catch(e => { if (active) setError(errorText(e)); });
    return () => { active = false; clearTimeout(timer); };
  }, [graphId, agent.id]);
  async function action(fn) {
    setBusy(true); setError('');
    try { await fn(); } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  }
  function replace(s) { setSessions(rows => [s, ...rows.filter(r => r.id !== s.id)]); setSessionId(s.id); }
  const resources = spaces.map(s => ({ ...s, name: memoryNodes.find(n => n.id === s.id)?.data.title || s.name }));
  function memory(resource, key, value) {
    action(() => onSave(agent, current => {
      const previous = current.memories.find(m => memoryId(m) === resource.id) || { ...memoryBinding(resource.id), read: false, write: false };
      const next = { ...previous, [key]: value };
      return { memories: [...current.memories.filter(m => memoryId(m) !== resource.id), ...(next.read || next.write ? [next] : [])] };
    }));
  }
  return <aside className="inspector memory-inspector">
    <header className="memory-inspector-header">
      <button className="link" onClick={page === 'settings' ? () => setPage('chat') : onBack}>{page === 'settings' ? '← Back to chat' : backLabel}</button>
      <h3>{agent.name}</h3>
      <span className="muted small">Independent Agent · Deep Agents + LangGraph</span>
    </header>
    <div className="memory-inspector-body">
      {error && <p role="alert">{error}</p>}
      {saving && <p role="status">Saving connections and settings…</p>}
      {page === 'settings' ? <>
        <div className="field"><label>Name<input value={draft.name} onChange={e => setDraft({ ...draft, name: e.target.value })} /></label></div>
        <div className="field"><label>Instructions<textarea rows={5} value={draft.instructions} onChange={e => setDraft({ ...draft, instructions: e.target.value })} /></label></div>
        <div className="field"><label>Model provider<input placeholder="Platform default" value={draft.provider || ''} onChange={e => setDraft({ ...draft, provider: e.target.value || null })} /></label></div>
        <div className="field"><label>Maximum model steps<input type="number" min="1" max="100" value={draft.max_steps} onChange={e => setDraft({ ...draft, max_steps: Number(e.target.value) })} /></label></div>
        <div className="field"><label>Time limit (seconds)<input type="number" min="10" max="1800" value={draft.timeout} onChange={e => setDraft({ ...draft, timeout: Number(e.target.value) })} /></label></div>
        <h4>Tools</h4>
        <div className="field"><label>Tool access<select value={draft.tools == null && draft.toolkits == null ? 'all' : 'selected'} onChange={e => setDraft({ ...draft, toolkits: null, tools: e.target.value === 'all' ? null : (draft.tools || toolkits.filter(t => t.available && (draft.toolkits == null || draft.toolkits.includes(t.name))).flatMap(t => (t.tools || []).map(x => x.name))) })}>
          <option value="all">All available tools (default)</option><option value="selected">Selected tools only</option>
        </select></label></div>
        {capabilitiesLoading && <p role="status">Loading tools and skills…</p>}
        {draft.tools == null && draft.toolkits == null ? <p className="muted small">Includes newly added platform tools automatically. Memory access follows the connections below.</p> : <>
          <div className="field"><label>Find tools<input value={toolSearch} onChange={e => setToolSearch(e.target.value)} /></label></div>
          <button type="button" onClick={() => setDraft({...draft, toolkits:null, tools:[]})}>Clear tool selection</button>
          {toolkits.filter(t => t.available).map(t => <div key={t.name}><strong>{t.name}</strong>{(t.tools || []).filter(x => `${x.name} ${x.description || ''}`.toLowerCase().includes(toolSearch.toLowerCase())).map(x => {
            const selected = draft.tools || toolkits.filter(k => draft.toolkits == null || draft.toolkits.includes(k.name)).flatMap(k => (k.tools || []).map(v => v.name));
            return <label className="field" key={x.name} title={x.description}><span><input type="checkbox" checked={selected.includes(x.name)} onChange={e => setDraft({...draft, toolkits:null, tools:e.target.checked ? [...selected,x.name] : selected.filter(n => n !== x.name)})} />{x.name}</span></label>;
          })}</div>)}
          <p className="muted small">An empty selection disables platform tool calls. Planning and connected memory remain available.</p>
        </>}
        <h4>Skills</h4>
        <p className="muted small">Selected skills are included in the instructions for each message. Manage skill content in Library.</p>
        {!capabilitiesLoading && !skills.length && <p>No skills in Library yet.</p>}
        {[...skills, ...(draft.skill_names || []).filter(n => !skills.some(s => s.name === n)).map(name => ({name,description:'Unavailable — remove or restore in Library'}))].map(s => <label className="field" key={s.name} title={s.description}><span><input type="checkbox" checked={(draft.skill_names || []).includes(s.name)} onChange={e => setDraft({...draft, skill_names:e.target.checked ? [...(draft.skill_names || []),s.name] : (draft.skill_names || []).filter(n => n !== s.name)})} />{s.name}</span><span className="muted small">{s.description}</span></label>)}
        <h4>Connected memories</h4>
        {!resources.length && <p>No Memory resources are available in this task.</p>}
        <p className="muted small">Connections save immediately. Read retrieves before each message; Write saves successful exchanges.</p>
        {resources.map(s => <div className="field" key={s.id}><strong>{s.name}</strong>{['read', 'write'].map(key => <label key={key}><input type="checkbox" disabled={busy || (key === 'write' && !s.writable)} aria-label={`${s.name} ${key}`} checked={!!agent.memories.find(m => memoryId(m) === s.id)?.[key]} onChange={e => memory(s, key, e.target.checked)} />{key === 'read' ? 'Read' : 'Write'}</label>)}{!s.writable && <p className="muted small">{s.write_reason}</p>}</div>)}
        <button disabled={busy || !draft.name.trim()} onClick={() => action(async () => { const patch = Object.fromEntries(Object.entries(draft).filter(([k, v]) => !['id', 'memories'].includes(k) && JSON.stringify(v) !== JSON.stringify(baseline[k]))); const saved = await onSave(agent, patch); setDraft(saved); setBaseline(saved); setPage('chat'); })}>Save settings</button>
        <p className="muted small">Settings apply to the next message. An active execution keeps its starting configuration.</p>
      </> : <>
        <div className="memory-row"><button onClick={() => setPage('settings')}>Harness settings →</button><button disabled={busy} onClick={() => action(async () => replace(await api.createAgentSession(graphId, agent.id)))}>New session</button></div>
        {!!sessions.length && <label>Session<select value={sessionId || ''} onChange={e => { setSessionId(e.target.value); setConfirmDelete(false); }}>{sessions.map(s => <option key={s.id} value={s.id}>{new Date(s.created_at * 1000).toLocaleString()} · {s.status}</option>)}</select></label>}
        {!!sessions.length && <details className="conversation-history" onToggle={e => setHistoryOpen(e.currentTarget.open)}><summary>Conversation history · {sessions.length}</summary>{historyOpen && sessions.map(s => <ResourceActions key={s.id} name={s.messages[0]?.content?.slice(0, 50) || 'New conversation'} items={[
          { label: 'Open conversation', action: () => { setSessionId(s.id); setConfirmDelete(false); } },
          { label: 'Delete conversation…', danger: true, disabled: busy || ['running', 'stopping'].includes(s.status), action: () => { setSessionId(s.id); setConfirmDelete(true); } },
        ]}><button className="conversation-history-row" onClick={() => { setSessionId(s.id); setConfirmDelete(false); }}><strong>{s.messages[0]?.content?.slice(0, 50) || 'New conversation'}</strong><small>{new Date(s.created_at * 1000).toLocaleString()} · {s.status}</small></button></ResourceActions>)}</details>}
        {session && <div className="session-delete-actions">{confirmDelete ? <>
          <p>Delete this conversation and its saved execution state? Shared Memory entries are retained.</p>
          <button disabled={busy} onClick={() => setConfirmDelete(false)}>Cancel</button>
          <button className="danger" disabled={busy || running} onClick={() => action(async () => {
            const id = session.id;
            await api.deleteAgentSession(graphId, agent.id, id);
            deletedSessions.current.add(id);
            const remaining = sessions.filter(s => s.id !== id);
            setSessions(remaining); setSessionId(remaining[0]?.id || null); setConfirmDelete(false);
          })}>Confirm delete</button>
        </> : <button disabled={busy || running} title={running ? 'Stop this conversation before deleting it' : 'Delete this conversation'} onClick={() => setConfirmDelete(true)}>Delete conversation</button>}</div>}
        {!session?.messages.length && <p>Chat here to run this Agent independently. It can discover tools and use connected memories.</p>}
        <div className="field"><strong>Connected Memory</strong>{!agent.memories.length && <p>No Memory connected.</p>}{agent.memories.map(m => <div key={memoryId(m)}>{resources.find(r => r.id === memoryId(m))?.name || memoryNodes.find(n => n.id === memoryId(m))?.data.title || memoryId(m)} · {[m.read && 'Read', m.write && 'Write'].filter(Boolean).join(' / ')}</div>)}</div>
        <div aria-live="polite">{session?.messages.map((m, i) => <article className="agent-message" key={i}><strong>{m.role === 'user' ? 'You' : agent.name}</strong><div>{m.content}</div>{m.failed && <small>Previous attempt failed; not replayed.</small>}</article>)}</div>
        {session?.events.length > 0 && <details><summary>Execution activity ({session.events.length})</summary>{session.events.map((e, i) => <div className="agent-event" key={i}><strong>{e.type} {e.name || ''}</strong>{e.type === 'memory_read' && <span> · {e.hits} records{e.total_records != null ? ` / ${e.total_records} stored` : ''}{e.mode === 'browse' ? ' · overview preview' : ''}</span>}<pre>{JSON.stringify(e.args || e.items || e.content, null, 2)}</pre></div>)}</details>}
        {session?.error && <p role="alert">{session.error}</p>}
        {running && <p role="status">{session.status === 'stopping' ? 'Stopping after the current operation…' : 'Agent is running…'}</p>}
        <textarea aria-label="Message Chat Agent" rows={4} value={message} onChange={e => setMessage(e.target.value)} placeholder="Ask this Agent to investigate, use a tool, or recall a memory…" />
        <div className="memory-row"><button disabled={saving || busy || running || !message.trim()} onClick={() => action(async () => {
          const current = session || await api.createAgentSession(graphId, agent.id);
          const result = await api.sendAgentMessage(graphId, agent.id, current.id, message);
          replace(result); setMessage('');
        })}>Send</button>{running && <button disabled={busy || session.status === 'stopping'} onClick={() => action(async () => replace(await api.stopAgentSession(graphId, agent.id, session.id)))}>Stop</button>}</div>
        <p className="muted small">Runs independently of workflow Run. Conversations are saved on the server.</p>
      </>}
    </div>
  </aside>;
}
