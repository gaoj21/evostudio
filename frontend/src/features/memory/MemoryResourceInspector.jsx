import React, { useState } from 'react';
import { Mem0SpacePicker, Mem0Manager } from './Mem0Spaces.jsx';

export default function MemoryResourceInspector({ resource, graphId, nodes, edges, onAdd, onConnect, onDisconnect, onRemove, onEditAgent, onBack, runMode }) {
  const [space, setSpace] = useState('');
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState('');
  async function addToCanvas() {
    if (adding || !space) return;
    setAdding(true); setError('');
    try { await onAdd(space); }
    catch (e) { setError(e?.body?.detail || e.message); }
    finally { setAdding(false); }
  }
  const [agent, setAgent] = useState('');
  const [manage, setManage] = useState(false);
  const [entriesOpened, setEntriesOpened] = useState(false);
  const agents = nodes.filter(n => !n.data.kind || n.data.kind === 'task');
  const data = resource?.data;
  const connections = edges.filter(e => e.data?.resource === resource?.id);
  const connected = [...new Set(connections.map(e => e.data.agent))];
  const header = <header className="memory-inspector-header">
    <button type="button" className="link" onClick={manage ? () => setManage(false) : onBack}>
      {manage ? '← Back to memory' : '← Back to workflow'}
    </button>
    <h3>{manage ? 'Memory entries' : data?.title || 'Add shared memory'}</h3>
    {data && <span className="muted small">{manage ? data.title : data.kind === 'mem0' ? 'Shared Mem0 space' : `${data.kind} · ${data.owner}`}</span>}
  </header>;
  if (!resource) return <aside className="inspector memory-inspector">
    {header}
    <div className="memory-inspector-body">
      <p>Choose a shared space to place on the canvas.</p>
      <Mem0SpacePicker graphId={graphId} value={space} onChange={setSpace} />
      <button type="button" className="primary" disabled={!space || runMode || adding} onClick={addToCanvas}>{adding ? 'Adding…' : 'Add to canvas'}</button>
      {error && <div role="alert">{String(error)}</div>}
    </div>
  </aside>;
  const selectedEdges = connections.filter(e => e.data.agent === agent);
  return <aside className="inspector memory-inspector">
    {header}
    <div className="memory-inspector-body" hidden={manage}>
      {data.kind === 'mem0' && !runMode && <button className="memory-open-entries" type="button" onClick={() => { setEntriesOpened(true); setManage(true); }}>Browse and edit memories →</button>}
      <section className="memory-inspector-section">
        <h4>Connected Agents <span className="muted">{connected.length}</span></h4>
        <p className="muted small">Read before execution · write after the run.</p>
        {!connected.length && <p className="muted">No connections yet.</p>}
        {connected.map(id => <div key={id} className="memory-agent-card">
          <button type="button" className="link memory-agent-name" onClick={() => onEditAgent(id)} title="Open Agent settings">{nodes.find(n => n.id === id)?.data.title || id} →</button>
          <div className="memory-direction-controls">{['read', 'write'].map(direction => {
            const edge = connections.find(e => e.data.agent === id && e.data.memory === direction);
            return <label key={direction}><input type="checkbox" aria-label={`${id} ${direction}`} checked={!!edge}
              disabled={runMode || (!edge && direction === 'write' && data.kind !== 'mem0' && data.owner !== id)}
              onChange={() => edge ? onDisconnect(edge) : onConnect(id, direction)} />{direction === 'read' ? 'Read' : 'Write'}</label>;
          })}</div>
        </div>)}
      </section>
      {!runMode && <section className="memory-inspector-section">
        <h4>Add a connection</h4>
        <select aria-label="Connect an Agent" value={agent} onChange={e => setAgent(e.target.value)}>
          <option value="">Choose Agent</option>{agents.map(n => <option key={n.id} value={n.id}>{n.data.title || n.id}</option>)}
        </select>
        <div className="memory-connect-actions">
          <button type="button" disabled={!agent || selectedEdges.some(e => e.data.memory === 'read')} onClick={() => onConnect(agent, 'read')}>Connect read</button>
          <button type="button" disabled={!agent || selectedEdges.some(e => e.data.memory === 'write') || (data.kind !== 'mem0' && data.owner !== agent)} onClick={() => onConnect(agent, 'write')}>Connect write</button>
        </div>
      </section>}
      {data.kind !== 'mem0' && <button type="button" onClick={() => onEditAgent(data.owner)}>Storage settings →</button>}
      {data.kind === 'mem0' && <details className="memory-inspector-section">
        <summary>Details and removal</summary>
        <p className="muted small memory-space-id">Space ID: {data.space_id}</p>
        {!runMode && <><button type="button" onClick={onRemove}>Remove from this canvas</button>
          <p className="muted small">Disconnects this workflow. Stored memories remain available.</p></>}
      </details>}
    </div>
    {entriesOpened && <div className="memory-inspector-body" hidden={!manage}>
      <Mem0Manager key={data.space_id} graphId={graphId} initialSpace={data.space_id} fixedSpace compact />
    </div>}
  </aside>;
}
