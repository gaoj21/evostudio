import React, { useCallback, useEffect, useMemo, useState } from 'react';
import Mem0Spaces from './Mem0Spaces.jsx';
import JsonView from '../../components/JsonView.jsx';
import { api } from '../../api.js';
import { unfold } from '../../jsonView.js';

/**
 * What the runs wrote to memory, per store.
 *
 * A table store (one row per company per date) reads as a timeline: pick
 * the node, optionally a company, and see its states in order. A corpus
 * (vector store) is searched. Nothing here refreshes on its own — a batch
 * writes as it goes — so there is a Refresh button and the list reloads
 * whenever the tab is opened.
 */
// One line per state, for the row heading: the scalar fields of the
// outputs, JSON-in-string unfolded — "alert · high · 82" for a decision.
function summary(content) {
  const outputs = unfold(content?.outputs ?? content);
  if (!outputs || typeof outputs !== 'object') return String(outputs ?? '');
  const parts = [];
  const walk = (v) => {
    if (v && typeof v === 'object' && !Array.isArray(v)) Object.values(v).forEach(walk);
    else if (v != null && typeof v !== 'object' && parts.length < 4) {
      const text = String(v);
      if (text.length <= 24) parts.push(text);
    }
  };
  walk(outputs);
  return parts.join(' · ') || '…';
}

const ALL = '__all__';

export default function MemoryPanel({ graphId }) {
  const [stores, setStores] = useState([]);
  const [store, setStore] = useState('');
  const [subject, setSubject] = useState('');
  const [query, setQuery] = useState('');
  const [entries, setEntries] = useState(null);
  const [kind, setKind] = useState('table');
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);
  const [tick, setTick] = useState(0);
  // Which subjects are unfolded. Everything starts folded: 7 companies × 6
  // states of JSON is a wall; the headings are the index.
  const [openSubjects, setOpenSubjects] = useState(() => new Set());
  const toggleSubject = (name) => setOpenSubjects((current) => {
    const next = new Set(current);
    if (next.has(name)) next.delete(name); else next.add(name);
    return next;
  });

  useEffect(() => {
    if (!graphId) return;
    api
      .listMemoryAgents(graphId)
      .then((res) => {
        const list = res.stores || (res.agents || []).map((a) => ({ node: a, kind: 'recall', subjects: [] }));
        setStores(list);
        // Two or more tables open together by default: what investigate
        // kept and what decide kept about the same company on the same
        // date belong side by side, not behind a dropdown.
        const tables = list.filter((s) => s.kind === 'table');
        setStore((current) => {
          if (current === ALL && tables.length > 1) return ALL;
          if (list.some((s) => s.node === current)) return current;
          return tables.length > 1 ? ALL : (list[0]?.node || '');
        });
      })
      .catch(() => setStores([]));
  }, [graphId, tick]);

  const tables = useMemo(() => stores.filter((s) => s.kind === 'table'), [stores]);
  const current = useMemo(() => (store === ALL
    ? { kind: 'table', subjects: [...new Set(tables.flatMap((s) => s.subjects || []))].sort() }
    : stores.find((s) => s.node === store)), [stores, store, tables]);

  useEffect(() => {
    if (!graphId || !store) return;
    setLoading(true);
    setError(null);
    const q = current?.kind === 'table' ? (subject || undefined) : (query || undefined);
    const fetches = store === ALL
      ? Promise.all(tables.map((s) => api.searchMemory(graphId, s.node, q)
        .then((res) => (res.entries || []).map((e) => ({ ...e, node: s.node })))))
        .then((lists) => ({ kind: 'table', entries: lists.flat() }))
      : api.searchMemory(graphId, store, q)
        .then((res) => ({ ...res, entries: (res.entries || []).map((e) => ({ ...e, node: store })) }));
    fetches
      .then((res) => {
        setEntries(res.entries || []);
        setKind(res.kind || current?.kind || 'recall');
      })
      .catch((err) => {
        setEntries([]);
        setError(err?.body?.detail || err.message);
      })
      .finally(() => setLoading(false));
  }, [graphId, store, subject, query, current?.kind, tick, tables]);

  const refresh = useCallback(() => setTick((t) => t + 1), []);

  // Table rows grouped by subject, then by date: one row per date holding
  // what each node kept that day, nodes in the order the stores are listed.
  const nodeOrder = useMemo(() => tables.map((s) => s.node), [tables]);
  const groups = useMemo(() => {
    if (kind !== 'table') return [];
    const bySubject = new Map();
    for (const e of entries || []) {
      if (!bySubject.has(e.subject)) bySubject.set(e.subject, new Map());
      const byDate = bySubject.get(e.subject);
      if (!byDate.has(e.at)) byDate.set(e.at, { at: e.at, recorded_at: e.recorded_at, cells: [] });
      byDate.get(e.at).cells.push(e);
    }
    const rank = (node) => { const i = nodeOrder.indexOf(node); return i < 0 ? 99 : i; };
    return [...bySubject.keys()].sort().map((subject) => ({
      subject,
      rows: [...bySubject.get(subject).values()]
        .sort((a, b) => String(a.at).localeCompare(String(b.at)))
        .map((row) => ({ ...row, cells: row.cells.sort((a, b) => rank(a.node) - rank(b.node)) })),
    }));
  }, [entries, kind, nodeOrder]);

  return (
    <div className="memory-panel">
      <Mem0Spaces key={graphId} graphId={graphId} />
      <div className="memory-controls">
        <select value={store} onChange={(e) => { setStore(e.target.value); setSubject(''); }} aria-label="Memory store">
          {stores.length === 0 && <option value="">(no memory stores yet)</option>}
          {tables.length > 1 && (
            <option value={ALL}>all tables · {tables.map((s) => s.node).join(' + ')}</option>
          )}
          {stores.map((s) => (
            <option key={s.node} value={s.node}>
              {s.node} · {s.kind === 'table' ? `table · ${s.count ?? '?'} rows` : 'corpus'}
            </option>
          ))}
        </select>
        {current?.kind === 'table' ? (
          <select value={subject} onChange={(e) => setSubject(e.target.value)} aria-label="Subject">
            <option value="">all {current.subjects?.length || 0} subjects</option>
            {(current.subjects || []).map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        ) : (
          <input
            placeholder="Search memories (empty = list all)…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        )}
        <button type="button" onClick={refresh} title="Reload from the server">Refresh</button>
        {kind === 'table' && groups.length > 0 && (
          <button
            type="button"
            className="link small"
            onClick={() => setOpenSubjects(openSubjects.size === groups.length ? new Set() : new Set(groups.map((g) => g.subject)))}
          >
            {openSubjects.size === groups.length ? 'Collapse all' : 'Expand all'}
          </button>
        )}
      </div>
      {error && <div className="muted small batch-error">{String(error)}</div>}
      <div className="memory-entries">
        {loading && <div className="muted small">Loading…</div>}
        {!loading && entries && entries.length === 0 && (
          <div className="muted small">No memory entries.</div>
        )}
        {kind === 'table' && groups.map((g) => {
          const open = openSubjects.has(g.subject);
          return (
            <div className="memory-subject" key={g.subject}>
              <button
                type="button"
                className="memory-subject-head"
                onClick={() => toggleSubject(g.subject)}
                aria-expanded={open}
              >
                <span className="memory-arrow">{open ? '▾' : '▸'}</span>
                <strong>{g.subject}</strong>
                <span className="muted small">{g.rows.length} {g.rows.length === 1 ? 'state' : 'states'}</span>
                <span className="muted small memory-span">{g.rows[0].at} → {g.rows[g.rows.length - 1].at}</span>
              </button>
              {open && g.rows.map((row) => (
                <div className="memory-entry" key={`${g.subject}|${row.at}`}>
                  <div className="memory-entry-head muted small">
                    <span className="memory-at">{row.at}</span>
                    <span className="memory-recorded">recorded {String(row.recorded_at || '').slice(0, 19).replace('T', ' ')}</span>
                  </div>
                  {row.cells.map((e) => (
                    <div className="memory-cell" key={e.node}>
                      <div className="memory-cell-head small">
                        <span className="memory-cell-node">{e.node}</span>
                        <span className="memory-summary muted" title={summary(e.content)}>{summary(e.content)}</span>
                      </div>
                      <JsonView value={e.content?.outputs ?? e.content} className="batch-output" startOpen={false} />
                    </div>
                  ))}
                </div>
              ))}
            </div>
          );
        })}
        {kind !== 'table' && (entries || []).map((e, i) => (
          <div className="memory-entry" key={e.memory_id || i}>
            <div className="memory-entry-head muted small">
              <span>{e.timestamp || ''}</span>
              {e.wf_task && <span> · task: {e.wf_task}</span>}
              {e.msg_type && <span> · {e.msg_type}</span>}
            </div>
            <JsonView value={e.content} className="batch-output" />
          </div>
        ))}
      </div>
    </div>
  );
}
