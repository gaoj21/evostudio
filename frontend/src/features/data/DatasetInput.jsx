import NumberInput from '../../components/NumberInput.jsx';
import React, { useEffect, useRef, useState } from 'react';
import { api } from '../../api.js';
import JsonView from '../../components/JsonView.jsx';

const message = err => err.body?.detail || err.message;

export default function DatasetInput({ config, onChange }) {
  const [datasets, setDatasets] = useState([]);
  const [selected, setSelected] = useState(null);
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [confirmDelete, setConfirmDelete] = useState(false);
  const fileRef = useRef(null);

  useEffect(() => {
    let active = true;
    api.listDatasets().then(r => { if (active) setDatasets(r.datasets); })
      .catch(err => { if (active) setError(message(err)); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    let active = true;
    setSelected(null); setConfirmDelete(false); setError('');
    if (config.dataset_id) {
      api.getDataset(config.dataset_id).then(item => {
        if (active) { setSelected(item); setName(item.name); }
      }).catch(err => { if (active) setError(message(err)); });
    }
    return () => { active = false; };
  }, [config.dataset_id]);

  const emit = (next, fields) => onChange(next, !next.dataset_id ? [] : next.input_mode === 'reference'
    ? [{name: next.reference_field || 'obligor_list', type: 'list', description: 'Shared reference records', required: false}]
    : fields.map(field => { const original = Object.entries(next.field_mapping || {}).find(([,target]) => target === field)?.[0] || field; return { name: field, type: next.column_types?.[original] || next.field_types?.[original] || selected?.field_types?.[original] || 'str', description: 'Dataset field', required: false }; }));
  const update = (item, mapping) => {
    const mapped = mapping || Object.fromEntries((item?.fields || []).map(f => [f, f]));
    emit({ ...config, dataset_id: item?.id || '', field_mapping: mapped, field_types: item?.field_types || {}, column_types: item?.id === config.dataset_id ? config.column_types || {} : {} }, Object.values(mapped));
  };

  async function upload(file) {
    if (!file) return;
    setError('');
    setBusy(true);
    try {
      const item = await api.uploadDataset(file);
      setDatasets(previous => [item, ...previous]);
      update(item);
    } catch (err) { setError(message(err)); }
    finally { setBusy(false); }
  }

  async function rename() {
    setBusy(true); setError('');
    try {
      const item = await api.renameDataset(selected.id, name);
      setSelected(item);
      setDatasets(previous => previous.map(d => d.id === item.id ? item : d));
    } catch (err) { setError(message(err)); }
    finally { setBusy(false); }
  }

  async function remove() {
    setBusy(true); setError('');
    try {
      await api.deleteDataset(selected.id);
      setDatasets(previous => previous.filter(d => d.id !== selected.id));
      update(null);
    } catch (err) { setError(message(err)); }
    finally { setBusy(false); }
  }

  const mapping = config.field_mapping || Object.fromEntries((selected?.fields || []).map(f => [f, f]));
  return <section aria-label="My dataset" className="dataset-input">
    <div className="input-step">
      <label htmlFor="input-dataset">1. Choose data</label>
      <select id="input-dataset" aria-label="My datasets" value={config.dataset_id || ''} disabled={busy}
        onChange={e => update(datasets.find(d => d.id === e.target.value))}>
        <option value="">Choose a saved dataset</option>
        {config.dataset_id && !datasets.some(d => d.id === config.dataset_id) && <option value={config.dataset_id}>Dataset unavailable</option>}
        {datasets.map(d => <option key={d.id} value={d.id}>{d.name} · {d.row_count} records</option>)}
      </select>
      <input ref={fileRef} aria-label="Upload dataset file" type="file" hidden accept=".csv,.tsv,.json,.jsonl"
        onChange={e => { upload(e.target.files?.[0]); e.target.value = ''; }} />
      <button className="input-upload" type="button" disabled={busy} onClick={() => fileRef.current?.click()}>{busy ? 'Saving…' : '+ Upload dataset'}</button>
      <span className="muted small">CSV, TSV, JSON or JSONL · no fixed size limit</span>
    </div>
    {error && <p role="alert" className="error">{error}</p>}
    {selected && <>
      <div className="input-step">
        <label htmlFor="dataset-input-mode">2. How to use it</label>
        <select id="dataset-input-mode" aria-label="Input role" value={config.input_mode || 'records'} disabled={busy}
          onChange={e => emit({...config, input_mode:e.target.value, reference_field:config.reference_field || 'obligor_list'}, Object.values(mapping))}>
          <option value="records">Process each record · news, transactions</option>
          <option value="reference">Share the whole list · obligors, reference data</option>
        </select>
        <p className="input-hint">{config.input_mode === 'reference'
          ? 'Every run receives this complete list.'
          : 'Each record becomes one workflow run.'}</p>
      </div>
      <div className="input-ready"><strong>{selected.row_count} records ready</strong><span>{selected.fields.length} fields · connect this Input to an agent</span></div>
      <details className="input-disclosure"><summary>Preview data</summary>
        <div style={{ maxHeight: 220, overflow: 'auto' }}><JsonView value={selected.preview} /></div>
      </details>
      <details className="input-disclosure"><summary>Advanced settings{config.n > 0 ? ` · first ${config.n} records` : ''}</summary>
        {config.input_mode === 'reference' && <div className="field">
          <label htmlFor="reference-output">Reference output name</label>
          <input id="reference-output" value={config.reference_field ?? 'obligor_list'}
            onChange={e => emit({...config, reference_field:e.target.value}, Object.values(mapping))} />
          <p className="muted small">Use this name when connecting the list to an agent.</p>
        </div>}
        <div className="field">
          <label htmlFor="dataset-limit">Record limit (0 = all)</label>
          <NumberInput id="dataset-limit" type="number" min="0" step="1" value={config.n ?? 0}
            onChange={e => onChange({ ...config, n: Number(e.target.value) })} />
        </div>
        <details><summary>Field mapping</summary>
          <p className="muted small">Keep the original names unless a connection needs different names.</p>
          {selected.fields.map(field => <div className="field" key={field}>
            <label htmlFor={`dataset-field-${field}`}>{field} → output</label>
            <input id={`dataset-field-${field}`} value={mapping[field] ?? field} disabled={busy}
              onChange={e => update(selected, { ...mapping, [field]: e.target.value })} />
            <label htmlFor={`dataset-type-${field}`}>Column type</label>
            <select id={`dataset-type-${field}`} aria-label={`${field} type`} value={config.column_types?.[field] || ''}
              onChange={e => { const column_types = {...config.column_types}; if (e.target.value) column_types[field] = e.target.value; else delete column_types[field]; emit({...config, column_types}, Object.values(mapping)); }}>
              <option value="">Original ({selected.field_types?.[field] || 'str'})</option>
              {['str', 'int', 'float', 'bool', 'list', 'dict'].map(type => <option key={type} value={type}>{type}</option>)}
            </select>
          </div>)}
          <p className="muted small">Conversions apply to this Input only. Empty values become null for non-text types; invalid values stop the run with a row and column error.</p>
        </details>
      </details>
      <details className="input-disclosure"><summary>Manage dataset</summary>
        <p className="muted small">This dataset is shared across tasks. Detaching only changes this Input.</p>
        {!!selected.used_by?.length && <p className="muted small">Used by: {selected.used_by.map(use => `${use.graph_name} / ${use.node}`).join(', ')}</p>}
        <div className="field">
          <label htmlFor="dataset-name">Dataset name</label>
          <input id="dataset-name" maxLength={120} value={name} disabled={busy} onChange={e => setName(e.target.value)} />
          <div className="input-actions">
            <button type="button" disabled={busy || !name.trim() || name === selected.name} onClick={rename}>Rename</button>
            <button type="button" disabled={busy} onClick={() => update(null)}>Detach from this Input</button>
            <button type="button" disabled={busy} onClick={() => setConfirmDelete(true)}>Delete dataset</button>
          </div>
        </div>
        {confirmDelete && <div role="alert">
          <p>Delete “{selected.name}”? Deletion is blocked while a saved task still uses it. Detach it and save those tasks first. Existing run results are kept.</p>
          <button type="button" disabled={busy} onClick={remove}>Confirm delete</button>
          <button type="button" disabled={busy} onClick={() => setConfirmDelete(false)}>Cancel</button>
        </div>}
      </details>
    </>}
  </section>;
}
