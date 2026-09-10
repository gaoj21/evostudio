import React, { useEffect, useState } from 'react';
import ResourceActions from '../../components/ResourceActions.jsx';
import { api } from '../../api.js';

const errorText = e => e?.body?.detail || e.message;

export function Mem0SpacePicker({ graphId, value = '', onChange }) {
  const [spaces, setSpaces] = useState([]);
  const [name, setName] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let active = true;
    if (graphId) api.mem0Spaces(graphId).then(r => { if (active) setSpaces(r.spaces); })
      .catch(e => { if (active) setError(errorText(e)); });
    return () => { active = false; };
  }, [graphId]);
  async function create() {
    setBusy(true); setError('');
    try {
      const s = await api.createMem0Space(graphId, name.trim());
      setSpaces(old => [...old, s]); setName(''); onChange(s.id);
    } catch (e) { setError(errorText(e)); }
    finally { setBusy(false); }
  }
  return <div className="field">
    <ResourceActions name={spaces.find(s => s.id === value)?.name || 'Shared spaces'} items={[
      { label: 'Delete selected space…', danger: true, disabled: busy || !value, action: async () => {
        if (!window.confirm('Delete this shared space and all its memories? Connected spaces must be disconnected first.')) return;
        setBusy(true); setError('');
        try { await api.deleteMem0Space(graphId, value); setSpaces(rows => rows.filter(s => s.id !== value)); onChange(''); }
        catch (e) { setError(errorText(e)); }
        finally { setBusy(false); }
      } },
    ]}><label>Mem0 space<select aria-label="Mem0 space" disabled={busy} value={value} onChange={e => onChange(e.target.value)}>
      <option value="">Select a space</option>
      {spaces.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
    </select></label></ResourceActions>
    <div className="memory-row"><input aria-label="New Mem0 space name" placeholder="New shared space name" value={name} onChange={e => setName(e.target.value)} />
      <button type="button" disabled={!graphId || busy || !name.trim()} onClick={create}>Create space</button></div>
    <div className="muted small">Shared across tasks in this project. Unassigned tasks have isolated spaces. Creating a space does not call a model.</div>
    {error && <div role="alert">{String(error)}</div>}
  </div>;
}

export function Mem0Manager({ graphId, initialSpace = '', fixedSpace = false, compact = false }) {
  const [space, setSpace] = useState(initialSpace);
  const [query, setQuery] = useState('');
  const [search, setSearch] = useState('');
  const [entries, setEntries] = useState([]);
  const [content, setContent] = useState('');
  const [editing, setEditing] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let active = true;
    setEntries([]); setError(''); setLoading(!!space);
    if (space) api.mem0Entries(graphId, space, search).then(r => { if (active) setEntries(r.entries); })
      .catch(e => { if (active) setError(errorText(e)); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [graphId, space, search, tick]);
  async function write(action) {
    setBusy(true); setError('');
    try { await action(); setContent(''); setEditing(null); setTick(t => t + 1); }
    catch (e) { setError(errorText(e)); }
    finally { setBusy(false); }
  }
  return <section aria-label="Mem0 shared memory">
    {!compact && <p>Mem0 · local persistent storage and embeddings · raw writes, no extraction-model calls.</p>}
    <fieldset disabled={busy}>
      {!fixedSpace && <Mem0SpacePicker graphId={graphId} value={space} onChange={id => { setSpace(id); setContent(''); setEditing(null); setSearch(''); setQuery(''); }} />}
      {space && <>
        <div className="memory-row"><input aria-label="Search Mem0" value={query} onChange={e => setQuery(e.target.value)} />
          <button type="button" onClick={() => { setSearch(query); setTick(t => t + 1); }}>Search / refresh</button></div>
        <p className="muted small">Shows up to 100 entries. Search to narrow the results. Workflow memory reset does not clear shared spaces.</p>
        {loading && <p role="status">Loading memories…</p>}
        {!loading && !entries.length && !error && <p>No memories found.</p>}
        {entries.map(row => <ResourceActions key={row.id} name={row.memory?.slice(0, 50) || 'Memory entry'} items={[
          { label: 'Edit memory', disabled: busy, action: () => { setEditing(row.id); setContent(row.memory); } },
          { label: 'Delete memory…', danger: true, disabled: busy, action: () => { if (window.confirm('Delete this memory from the shared space?')) write(() => api.deleteMem0Entry(graphId, space, row.id)); } },
        ]}><article className="field">
          <pre style={{ whiteSpace: 'pre-wrap' }}>{row.memory}</pre>
          <div className="muted small">{row.metadata?.node || 'Manual entry'} · {row.created_at}</div>
          <button type="button" onClick={() => { setEditing(row.id); setContent(row.memory); }}>Edit memory</button>
          <button type="button" onClick={() => { if (window.confirm('Delete this memory from the shared space?')) write(() => api.deleteMem0Entry(graphId, space, row.id)); }}>Delete memory</button>
        </article></ResourceActions>)}
        <label>{editing ? 'Edit memory content' : 'New memory content'}<textarea aria-label="Memory content" rows={4} value={content} onChange={e => setContent(e.target.value)} /></label>
        <button type="button" disabled={!content.trim()} onClick={() => write(() => editing ? api.updateMem0Entry(graphId, space, editing, content) : api.addMem0Entry(graphId, space, content))}>{editing ? 'Save memory' : 'Add memory'}</button>
        {editing && <button type="button" onClick={() => { setEditing(null); setContent(''); }}>Cancel edit</button>}
      </>}
    </fieldset>
    {error && <div role="alert">{String(error)}</div>}
  </section>;
}

export default function Mem0Spaces({ graphId }) {
  const [open, setOpen] = useState(false);
  return <details onToggle={e => setOpen(e.currentTarget.open)}><summary>Mem0 shared memory</summary>
    {open && graphId && <Mem0Manager key={graphId} graphId={graphId} />}
  </details>;
}
