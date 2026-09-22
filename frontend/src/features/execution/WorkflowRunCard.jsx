import React, { useState } from 'react';

export default function WorkflowRunCard({ proposal, onRun, onDismiss }) {
  const [values, setValues] = useState(() => Object.fromEntries(Object.entries(proposal.inputs || {}).map(([k, v]) => [k, typeof v === 'object' && v !== null ? JSON.stringify(v, null, 2) : String(v)])));
  const [record, setRecord] = useState(proposal.record ?? '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const specs = proposal.plan?.inputs || Object.keys(proposal.inputs || {}).map(name => ({ name, type: typeof proposal.inputs[name] === 'number' ? 'number' : 'str' }));
  async function run() {
    setError('');
    try {
      const inputs = { ...proposal.inputs };
      for (const field of specs) {
        const value = values[field.name] ?? '';
        if (value === '') {
          if (field.required !== false) throw new Error(`Provide ${field.description || field.name}.`);
          delete inputs[field.name]; continue;
        }
        if (['int', 'integer', 'float', 'number'].includes(field.type)) {
          const number = Number(value);
          if (!Number.isFinite(number) || (['int', 'integer'].includes(field.type) && !Number.isInteger(number))) throw new Error(`${field.name} needs a valid ${field.type}.`);
          inputs[field.name] = number;
        } else if (['bool', 'boolean'].includes(field.type)) inputs[field.name] = value === 'true';
        else if (['list', 'array', 'dict', 'object', 'json'].includes(field.type)) {
          try { inputs[field.name] = JSON.parse(value); } catch { throw new Error(`${field.name} needs valid JSON.`); }
        } else inputs[field.name] = value;
      }
      if (record !== '' && (!Number.isInteger(Number(record)) || Number(record) < 0)) throw new Error('Source record must be a non-negative whole number.');
      setBusy(true);
      await onRun({ ...proposal, inputs, record: record === '' ? undefined : Number(record) });
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  }
  return <div className="chat-confirm">
    <strong>Run this workflow?</strong>
    <p className="chat-note">Review inputs, then run the current design.</p>
    {proposal.start_at?.length > 0 && <p className="chat-note">Start at: {proposal.start_at.join(', ')}</p>}
    {proposal.plan?.nodes && <p className="chat-note">{proposal.plan.nodes.length} steps planned</p>}
    {specs.map(field => <label className="workflow-run-field" key={field.name}>{field.description || field.name}{field.required !== false ? ' *' : ''}
      {['bool', 'boolean'].includes(field.type) ? <select aria-label={field.name} disabled={busy} value={values[field.name] ?? ''} onChange={e => setValues(v => ({ ...v, [field.name]: e.target.value }))}><option value="">Choose…</option><option value="true">True</option><option value="false">False</option></select>
      : <textarea aria-label={field.name} rows={2} disabled={busy} value={values[field.name] ?? ''} onChange={e => setValues(v => ({ ...v, [field.name]: e.target.value }))} />}
    </label>)}
    {proposal.plan?.source && <label className="workflow-run-field">Source record (starting from 0)<input type="number" min="0" step="1" disabled={busy} value={record} onChange={e => setRecord(e.target.value)} /></label>}
    {(proposal.plan?.warnings || []).map((w, i) => <p className="chat-note" key={i}>{typeof w === 'string' ? w : JSON.stringify(w)}</p>)}
    {error && <p role="alert" className="chat-error">{error}</p>}
    <div className="chat-confirm-actions"><button type="button" className="primary" disabled={busy} onClick={run}>{busy ? 'Starting…' : 'Run it'}</button><button type="button" disabled={busy} onClick={onDismiss}>Not now</button></div>
  </div>;
}
