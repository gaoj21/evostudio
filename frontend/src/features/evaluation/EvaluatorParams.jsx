import React from 'react';
import NumberInput from '../../components/NumberInput.jsx';

// The form is the code's own signature: one field per declared parameter,
// typed as the code declares it, defaults prefilled, required ones marked.
export default function EvaluatorParams({ iface, values, onChange, disabled }) {
  const params = iface?.params || [];
  const update = (name, value) => {
    const next = { ...values };
    if (value === undefined) delete next[name]; else next[name] = value;
    onChange(next);
  };
  return <section aria-label="Evaluator parameters"><h4>Parameters</h4>
    {iface?.error && <pre role="alert" className="json-view run-error" data-testid="interface-error">{String(iface.error)}</pre>}
    {!iface && <p className="muted small">Check the code to read its parameters.</p>}
    {iface && !iface.error && <p className="muted small" data-testid="interface-kind">
      {iface.kind === 'factory' ? 'Factory entry point' : 'Evaluation function'}{iface.entrypoint ? `: ${iface.entrypoint}` : ''}
      {' · '}The saved results supply the records; fill in the parameters below.</p>}
    {iface && !iface.error && params.length === 0 && <p className="muted small">No parameters declared.</p>}
    {params.map((field) => {
      const value = values[field.name] ?? field.default ?? '';
      const id = `evaluator-arg-${field.name}`;
      return <div className="field" key={field.name}>
        <label htmlFor={id}>{field.name}{field.required ? ' *' : ''} <small>({field.type || 'any'})</small></label>
        {field.options ? <select id={id} disabled={disabled} value={JSON.stringify(value)} onChange={(e) => update(field.name, JSON.parse(e.target.value))}><option value={JSON.stringify('')}>Select…</option>{field.options.map((v) => <option key={JSON.stringify(v)} value={JSON.stringify(v)}>{String(v)}</option>)}</select>
        : field.type === 'bool' ? <select id={id} disabled={disabled} value={String(value)} onChange={(e) => update(field.name, e.target.value === '' ? undefined : e.target.value === 'true')}><option value="">Select…</option><option value="true">True</option><option value="false">False</option></select>
        : ['list', 'dict'].includes(field.type) ? <textarea id={id} disabled={disabled} defaultValue={value === '' ? '' : JSON.stringify(value, null, 2)} onBlur={(e) => { try { update(field.name, e.target.value ? JSON.parse(e.target.value) : undefined); e.target.setCustomValidity(''); } catch { e.target.setCustomValidity('Enter valid JSON'); e.target.reportValidity(); } }} />
        : ['int', 'float'].includes(field.type) ? <NumberInput id={id} disabled={disabled} required={field.required} step={field.type === 'int' ? 1 : 'any'} value={value} onChange={(e) => update(field.name, e.target.value === '' ? undefined : Number(e.target.value))} />
        : <input id={id} disabled={disabled} required={field.required} type="text" value={value} onChange={(e) => update(field.name, e.target.value)} />}
        {field.description && <small className="muted">{field.description}</small>}
      </div>;
    })}
  </section>;
}
