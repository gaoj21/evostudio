import NumberInput from '../../components/NumberInput.jsx';
import React, { useEffect, useRef, useState } from 'react';
import { memoryId, setMemory } from './chatAgents.js';
import ResourceActions from '../../components/ResourceActions.jsx';
import { api } from '../../api.js';
import CopyButton from '../chat/CopyButton.jsx';
const errorText = e => String(e.body?.detail || e.message);
// What a conversation runs with: these settings as last saved with the
// workflow. Edits are canvas edits (undoable, saved with the workflow).
const SETTINGS = ['description', 'instructions', 'provider', 'max_steps', 'timeout', 'toolkits', 'tools', 'skill_names', 'memories'];
const same = (a, b) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
// Which conversation was open, so reopening this Agent returns to it (and to a
// turn still running there) rather than to whichever session is newest.
const sessionKey = (graphId, agentId) => `evoagentx-studio:agent-session:${graphId}:${agentId}`;
const loadSessionId = (graphId, agentId) => { try { return localStorage.getItem(sessionKey(graphId, agentId)); } catch { return null; } };
function pickSession(sessions, preferred) {
  return sessions.find(s => s.id === preferred)?.id
    || sessions.find(s => ['running', 'stopping'].includes(s.status))?.id
    || sessions[0]?.id || null;
}
// The Chat Agent node's inspector: its settings (written into the node, so
// they mark the workflow unsaved and undo with it) and its conversations,
// which use the settings saved with the workflow (`saved`, null while the
// node has never been saved).
export default function ChatAgentInspector({ graphId, agent, saved = null, onBack, onChange, onRename, onSaveWorkflow,
  saving = false, runMode = false, memoryNodes = [], backLabel = '← Back to workflow' }) {
  const agentId = agent.agent_id;
  const [page, setPage] = useState('chat');
  const draft = agent;
  const setDraft = next => onChange?.(Object.fromEntries(SETTINGS.filter(k => k in next && !same(next[k], agent[k])).map(k => [k, next[k]])));
  const [name, setName] = useState(agent.name);
  useEffect(() => { setName(agent.name); }, [agent.name]);
  const commitName = () => { const wanted = name.trim(); if (wanted && wanted !== agent.name) onRename?.(wanted); else setName(agent.name); };
  const [toolkits, setToolkits] = useState([]);
  const [skills, setSkills] = useState([]);
  const [capabilitiesLoading, setCapabilitiesLoading] = useState(true);
  const [toolSearch, setToolSearch] = useState('');
  const [spaces, setSpaces] = useState([]);
  const [spacesLoaded, setSpacesLoaded] = useState(false);
  const [sessions, setSessions] = useState([]);
  const [sessionId, setSessionId] = useState(() => loadSessionId(graphId, agentId));
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const deletedSessions = useRef(new Set());
  const restoring = useRef(true);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [busy, setBusy] = useState(false);
  const session = sessions.find(s => s.id === sessionId);
  // Settings that differ from the saved ones (a rename is not one: the
  // conversation is the Agent's, by agent_id, whatever the node is called).
  const settingsDirty = !!saved && SETTINGS.some(k => !same(agent[k], saved[k]));
  useEffect(() => {
    try { if (sessionId) localStorage.setItem(sessionKey(graphId, agentId), sessionId); } catch { /* Browser storage may be unavailable. */ }
  }, [graphId, agentId, sessionId]);
  const running = ['running', 'stopping'].includes(session?.status);
  useEffect(() => {
    let active = true;
    Promise.all([api.listTools(), api.listSkills()]).then(([t, s]) => {
      if (active) { setToolkits(t.tools || []); setSkills(s.skills || []); }
    }).catch(e => { if (active) setError(errorText(e)); }).finally(() => { if (active) setCapabilitiesLoading(false); });
    return () => { active = false; };
  }, [graphId, agentId]);
  useEffect(() => {
    let active = true;
    let timer;
    async function refresh() {
      try {
        const r = await api.agentSessions(graphId, agentId);
        if (!active) return;
        const visible = r.sessions.filter(s => !deletedSessions.current.has(s.id));
        setSessions(visible);
        // The remembered id is checked against the server once; a session
        // created here since then may simply not be listed yet.
        const remembered = restoring.current;
        restoring.current = false;
        setSessionId(id => (id && (!remembered || visible.some(s => s.id === id))) ? id : pickSession(visible, id));
      } catch (e) { if (active) setError(errorText(e)); }
      if (active) timer = setTimeout(refresh, 1500);
    }
    refresh();
    (api.chatMemoryResources ? api.chatMemoryResources(graphId) : api.mem0Spaces(graphId).then(r => ({ resources: r.spaces.map(s => ({ ...s, id: `mem:space:${s.id}`, writable: true })) }))).then(r => { if (active) { setSpaces(r.resources); setSpacesLoaded(true); } }).catch(e => { if (active) setError(errorText(e)); });
    return () => { active = false; clearTimeout(timer); };
  }, [graphId, agentId]);
  async function action(fn) {
    setBusy(true); setError('');
    try { await fn(); } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  }
  function replace(s) { setSessions(rows => [s, ...rows.filter(r => r.id !== s.id)]); setSessionId(s.id); }
  const resources = spaces.map(s => ({ ...s, name: memoryNodes.find(n => n.id === s.id)?.data.title || s.name }));
  // Connections this task can no longer reach — typically shared memory of
  // the project the task was moved out of. Saving refuses them, so they are
  // shown and can be removed here.
  const available = m => resources.some(r => r.id === memoryId(m));
  const memories = agent.memories || [];
  const stale = spacesLoaded ? memories.filter(m => !available(m)) : [];
  const memoryName = m => resources.find(r => r.id === memoryId(m))?.name || memoryNodes.find(n => n.id === memoryId(m))?.data.title
    || (m.space_id ? `Shared memory ${m.space_id.slice(0, 8)}` : memoryId(m));
  function removeStale() { onChange?.({ memories: memories.filter(available) }); }
  function memory(resource, key, value) { onChange?.({ memories: setMemory(memories, resource.id, key, value) }); }
  const canChat = !!saved;
  const saveNote = !saved ? 'This Chat Agent is not saved yet. Save the workflow to chat with it.'
    : settingsDirty ? 'Unsaved settings: messages use the saved settings until you save the workflow.' : null;
  return <aside className="inspector memory-inspector">
    <header className="memory-inspector-header">
      <button className="link" onClick={page === 'settings' ? () => setPage('chat') : onBack}>{page === 'settings' ? '← Back to chat' : backLabel}</button>
      <h3>{agent.name}</h3>
      <span className="muted small">Chat Agent · not part of runs · LangGraph</span>
    </header>
    <div className="memory-inspector-body">
      {error && <p role="alert">{error}</p>}
      {saveNote && <div role="status" className="chat-agent-save-note"><p className="muted small">{saveNote}</p>
        {onSaveWorkflow && <button type="button" disabled={saving || runMode} onClick={onSaveWorkflow}>{saving ? 'Saving…' : 'Save workflow'}</button>}</div>}
      {page === 'settings' ? <fieldset className="chat-agent-settings" disabled={runMode}>
        <div className="field"><label>Name<input value={name} onChange={e => setName(e.target.value)} onBlur={commitName} onKeyDown={e => { if (e.key === 'Enter') e.currentTarget.blur(); }} /></label></div>
        <div className="field"><label>Description<input value={draft.description || ''} onChange={e => setDraft({ ...draft, description: e.target.value })} /></label></div>
        <div className="field"><label>Instructions<textarea rows={5} value={draft.instructions} onChange={e => setDraft({ ...draft, instructions: e.target.value })} /></label></div>
        <div className="field"><label>Model provider<input placeholder="Platform default" value={draft.provider || ''} onChange={e => setDraft({ ...draft, provider: e.target.value || null })} /></label></div>
        <div className="field"><label>Maximum model steps<NumberInput type="number" min="1" max="100" value={draft.max_steps} onChange={e => setDraft({ ...draft, max_steps: Number(e.target.value) })} /></label></div>
        <div className="field"><label>Time limit (seconds)<NumberInput type="number" min="10" max="1800" value={draft.timeout} onChange={e => setDraft({ ...draft, timeout: Number(e.target.value) })} /></label></div>
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
        <p className="muted small">Read retrieves before each message; Write saves successful exchanges.</p>
        {resources.map(s => <div className="field" key={s.id}><strong>{s.name}</strong>{['read', 'write'].map(key => <label key={key}><input type="checkbox" disabled={key === 'write' && !s.writable} aria-label={`${s.name} ${key}`} checked={!!memories.find(m => memoryId(m) === s.id)?.[key]} onChange={e => memory(s, key, e.target.checked)} />{key === 'read' ? 'Read' : 'Write'}</label>)}{!s.writable && <p className="muted small">{s.write_reason}</p>}</div>)}
        {stale.length > 0 && <div className="field" role="group" aria-label="Unavailable memories">
          <strong>Unavailable memories</strong>
          <p className="muted small">This task can no longer use these connections, for example shared memory of a project it was moved from. Chatting is refused until they are removed and the workflow saved.</p>
          {stale.map(m => <div key={memoryId(m)}>{memoryName(m)} · {[m.read && 'Read', m.write && 'Write'].filter(Boolean).join(' / ')}</div>)}
          <button type="button" onClick={removeStale}>Remove unavailable memories</button>
        </div>}
        <p className="muted small">Changes are part of the workflow: they are undone with it and apply to the next message once the workflow is saved. An active execution keeps its starting configuration.</p>
      </fieldset> : <>
        <div className="memory-row"><button onClick={() => setPage('settings')}>Harness settings →</button><button disabled={busy || !canChat} onClick={() => action(async () => replace(await api.createAgentSession(graphId, agentId)))}>New session</button></div>
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
            await api.deleteAgentSession(graphId, agentId, id);
            deletedSessions.current.add(id);
            const remaining = sessions.filter(s => s.id !== id);
            setSessions(remaining); setSessionId(remaining[0]?.id || null); setConfirmDelete(false);
          })}>Confirm delete</button>
        </> : <button disabled={busy || running} title={running ? 'Stop this conversation before deleting it' : 'Delete this conversation'} onClick={() => setConfirmDelete(true)}>Delete conversation</button>}</div>}
        {!session?.messages.length && <p>Chat here to run this Agent independently. It can discover tools and use connected memories.</p>}
        <div className="field"><strong>Connected Memory</strong>{!memories.length && <p>No Memory connected.</p>}{memories.map(m => <div key={memoryId(m)}>{memoryName(m)} · {[m.read && 'Read', m.write && 'Write'].filter(Boolean).join(' / ')}{stale.includes(m) && ' · unavailable (remove it in Harness settings)'}</div>)}</div>
        <div aria-live="polite">{session?.messages.map((m, i) => <article className="agent-message" key={i}><strong>{m.role === 'user' ? 'You' : agent.name}</strong><div>{m.content}</div>{m.content && <CopyButton text={m.content} label={m.role === 'user' ? 'Copy your message' : 'Copy reply'} />}{m.failed && <small>Previous attempt failed; not replayed.</small>}</article>)}</div>
        {session?.events.length > 0 && <details><summary>Execution activity ({session.events.length})</summary>{session.events.map((e, i) => <div className="agent-event" key={i}><strong>{e.type} {e.name || ''}</strong>{e.type === 'memory_read' && <span> · {e.hits} records{e.total_records != null ? ` / ${e.total_records} stored` : ''}{e.mode === 'browse' ? ' · overview preview' : ''}</span>}<pre>{JSON.stringify(e.args || e.items || e.content, null, 2)}</pre></div>)}</details>}
        {session && <p className="muted small">{session.token_usage?.reported_calls
          ? `Tokens (reported): ${session.token_usage.total_tokens} · input ${session.token_usage.input_tokens} / output ${session.token_usage.output_tokens} · this turn ${session.turn_token_usage?.total_tokens ?? 'unavailable'}`
          : 'Token usage: not reported by the provider yet.'}{session.usage_unavailable && ' Some model calls did not report usage; totals are partial.'}</p>}
        {session?.error && <p role="alert">{session.error}</p>}
        {running && <p role="status">{session.status === 'stopping' ? (session.stop_note || 'Stopping after the current operation…') : 'Agent is running…'}</p>}
        <textarea aria-label="Message Chat Agent" rows={4} value={message} onChange={e => setMessage(e.target.value)} placeholder="Ask this Agent to investigate, use a tool, or recall a memory…" />
        <div className="memory-row"><button disabled={!canChat || saving || busy || running || !message.trim()} onClick={() => action(async () => {
          const current = session || await api.createAgentSession(graphId, agentId);
          if (!session) replace(current);
          const result = await api.sendAgentMessage(graphId, agentId, current.id, message);
          replace(result); setMessage('');
        })}>Send</button>{running && <button disabled={busy || session.status === 'stopping'} onClick={() => action(async () => replace(await api.stopAgentSession(graphId, agentId, session.id)))}>Stop</button>}</div>
        <p className="muted small">Runs independently of workflow Run. Conversations are saved on the server.</p>
      </>}
    </div>
  </aside>;
}
