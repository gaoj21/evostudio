import React, { useEffect, useState } from 'react';
import { api } from '../api.js';

export default function MemoryPanel({ graphId }) {
  const [agents, setAgents] = useState([]);
  const [agent, setAgent] = useState('');
  const [query, setQuery] = useState('');
  const [entries, setEntries] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!graphId) return;
    api
      .listMemoryAgents(graphId)
      .then((res) => {
        setAgents(res.agents || []);
        if (res.agents?.length) setAgent((a) => a || res.agents[0]);
      })
      .catch(() => setAgents([]));
  }, [graphId]);

  useEffect(() => {
    if (!graphId || !agent) return;
    setLoading(true);
    setError(null);
    api
      .searchMemory(graphId, agent, query || undefined)
      .then((res) => setEntries(res.entries || []))
      .catch((err) => {
        setEntries([]);
        setError(err?.body?.detail || err.message);
      })
      .finally(() => setLoading(false));
  }, [graphId, agent, query]);

  return (
    <div className="memory-panel">
      <div className="memory-controls">
        <select value={agent} onChange={(e) => setAgent(e.target.value)}>
          {agents.length === 0 && <option value="">(no memory stores yet)</option>}
          {agents.map((a) => (
            <option key={a} value={a}>
              {a}
            </option>
          ))}
        </select>
        <input
          placeholder="Search memories (empty = list all)…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>
      {error && <div className="muted small batch-error">{String(error)}</div>}
      <div className="memory-entries">
        {loading && <div className="muted small">Loading…</div>}
        {!loading && entries && entries.length === 0 && (
          <div className="muted small">No memory entries.</div>
        )}
        {(entries || []).map((e, i) => (
          <div className="memory-entry" key={e.memory_id || i}>
            <div className="memory-entry-head muted small">
              <span>{e.timestamp || ''}</span>
              {e.wf_task && <span> · task: {e.wf_task}</span>}
              {e.msg_type && <span> · {e.msg_type}</span>}
            </div>
            <pre className="json-view batch-output">{String(e.content)}</pre>
          </div>
        ))}
      </div>
    </div>
  );
}
