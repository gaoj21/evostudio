import React, { useState } from 'react';
import { edgeToFlow, suggestMappings } from './convert.js';

export default function ConnectionEditor({ edge, nodes, edges, onSave, onDelete, onClose }) {
  const source = nodes.find(n => n.id === edge.source);
  const target = nodes.find(n => n.id === edge.target);
  const outputs = source?.data?.outputs || [];
  const inputs = target?.data?.inputs || [];
  const [orderOnly, setOrderOnly] = useState(!!edge.data?.control_only);
  const [mappings, setMappings] = useState(edge.data?.mappings || []);
  const occupied = new Map();
  for (const other of edges) {
    if (other.id !== edge.id && other.target === edge.target && !other.data?.control_only)
      for (const mapping of other.data?.mappings || []) occupied.set(mapping.to, other.source);
  }
  const invalid = mappings.some(m => !outputs.some(o => o.name === m.from)
    || !inputs.some(i => i.name === m.to) || occupied.has(m.to));
  const valid = source && target && (orderOnly || (mappings.length > 0 && !invalid));
  const update = (to, from) => setMappings(current => [...current.filter(m => m.to !== to), ...(from ? [{ from, to }] : [])]);
  return <div className="modal-backdrop" onClick={onClose}>
    <section className="modal connection-editor" role="dialog" aria-modal="true" aria-label="Edit connection"
      onClick={e => e.stopPropagation()} onKeyDown={e => { e.stopPropagation(); if (e.key === 'Escape') onClose(); }}>
      <h2>Edit connection</h2>
      <p>{edge.source} → {edge.target}</p>
      <label>Connection type <select autoFocus aria-label="Connection type" value={orderOnly ? 'order' : 'data'} onChange={e => setOrderOnly(e.target.value === 'order')}>
        <option value="data">Pass data</option><option value="order">Order only</option>
      </select></label>
      {orderOnly ? <p>Wait for the source to finish. No output data is passed along this connection.</p> : <>
        <p>Choose the source output for each target input. Field names can differ.</p>
        <button type="button" onClick={() => setMappings(suggestMappings(outputs, inputs).filter(m => !occupied.has(m.to)))}>Auto-match fields</button>
        {(!outputs.length || !inputs.length) && <p role="alert">Add outputs to the source and inputs to the target, or explicitly choose Order only.</p>}
        {inputs.map(input => <label className="connection-field" key={input.name}>
          <span>{input.name} <small>{input.type}</small></span>
          <select aria-label={`Source output for ${input.name}`} disabled={occupied.has(input.name)}
            value={mappings.find(m => m.to === input.name)?.from || ''} onChange={e => update(input.name, e.target.value)}>
            <option value="">{occupied.has(input.name) ? `Provided by ${occupied.get(input.name)}` : 'Not mapped on this connection'}</option>
            {outputs.map(output => <option key={output.name} value={output.name}>{output.name} ({output.type || 'any'})</option>)}
          </select>
        </label>)}
        {invalid && <p role="alert">Some mappings refer to removed fields or an input already supplied by another connection. Use Auto-match fields or update the mappings.</p>}
      </>}
      <div className="modal-actions">
        {onDelete && <button type="button" onClick={onDelete}>Delete connection</button>}
        <button type="button" onClick={onClose}>Cancel</button>
        <button type="button" className="primary" disabled={!valid} onClick={() => onSave(edgeToFlow({
          source: edge.source, target: edge.target, control_only: orderOnly, mappings: orderOnly ? [] : mappings,
        }))}>Save connection</button>
      </div>
    </section>
  </div>;
}
