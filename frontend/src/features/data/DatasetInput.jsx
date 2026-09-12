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
    : fields.map(field => ({ name: field, type: 'str', description: 'Dataset field', required: false })));
  const update = (item, mapping) => {
    const mapped = mapping || Object.fromEntries((item?.fields || []).map(f => [f, f]));
    emit({ ...config, dataset_id: item?.id || '', field_mapping: mapped }, Object.values(mapped));
  };

  async function upload(file) {
    if (!file) return;
    setError('');
    if (file.size > 20 * 1024 * 1024) { setError('File exceeds 20 MB. Split it before uploading.'); return; }
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
  return <section aria-label="My dataset">
    <div className="field">
      <label htmlFor="input-dataset">My datasets</label>
      <select id="input-dataset" value={config.dataset_id || ''} disabled={busy}
        onChange={e => update(datasets.find(d => d.id === e.target.value))}>
        <option value="">Choose a dataset</option>
        {config.dataset_id && !datasets.some(d => d.id === config.dataset_id) && <option value={config.dataset_id}>Dataset unavailable</option>}
        {datasets.map(d => <option key={d.id} value={d.id}>{d.name} · {d.row_count} records</option>)}
      </select>
    </div>
    <input ref={fileRef} aria-label="Upload dataset file" type="file" hidden accept=".csv,.tsv,.json,.jsonl"
      onChange={e => { upload(e.target.files?.[0]); e.target.value = ''; }} />
    <button type="button" disabled={busy} onClick={() => fileRef.current?.click()}>{busy ? 'Saving…' : '+ Upload dataset'}</button>
    <p className="muted small">CSV / TSV / JSON / JSONL · up to 20 MB and 50,000 records. Saved for reuse across tasks. Export Excel sheets as CSV.</p>
    {error && <p role="alert" className="error">{error}</p>}
    {selected && <>
      <div className="field">
        <label htmlFor="dataset-input-mode">Input role</label>
        <select id="dataset-input-mode" value={config.input_mode || 'records'} disabled={busy}
          onChange={e => emit({...config, input_mode:e.target.value, reference_field:config.reference_field || 'obligor_list'}, Object.values(mapping))}>
          <option value="records">Per-record input · e.g. news</option>
          <option value="reference">Shared reference · e.g. obligor list</option>
        </select>
        <p className="muted small">Use one per-record Input and connect additional shared references to the nodes that need them. Every news record receives the same reference list.</p>
      </div>
      {config.input_mode === 'reference' && <div className="field">
        <label htmlFor="reference-output">Reference output name</label>
        <input id="reference-output" value={config.reference_field ?? 'obligor_list'}
          onChange={e => emit({...config, reference_field:e.target.value}, Object.values(mapping))} />
        <p className="muted small">One list containing all selected rows, reused for every run. Connect this output to an agent input such as obligor_list. Use unique names for multiple reference lists.</p>
      </div>}
      <div className="field">
        <label htmlFor="dataset-name">Dataset name</label>
        <input id="dataset-name" maxLength={120} value={name} disabled={busy} onChange={e => setName(e.target.value)} />
        <button type="button" disabled={busy || !name.trim() || name === selected.name} onClick={rename}>Rename</button>
        <button type="button" disabled={busy} onClick={() => setConfirmDelete(true)}>Delete dataset</button>
      </div>
      {confirmDelete && <div role="alert">
        <p>Delete “{selected.name}”? Other tasks using this dataset will need a new input. Existing run results are kept.</p>
        <button type="button" disabled={busy} onClick={remove}>Confirm delete</button>
        <button type="button" disabled={busy} onClick={() => setConfirmDelete(false)}>Cancel</button>
      </div>}
      <p className="muted small">{selected.row_count} records · {selected.fields.length} fields. File order is preserved; missing JSON fields become null.</p>
      <details open><summary>Preview · first {selected.preview?.length || 0} records</summary>
        <div style={{ maxHeight: 240, overflow: 'auto' }}><JsonView value={selected.preview} /></div>
      </details>
      <details><summary>Field mapping</summary>
        <p className="muted small">Set the output names used by canvas connections. After renaming an output, update its downstream connection mapping.</p>
        {selected.fields.map(field => <div className="field" key={field}>
          <label htmlFor={`dataset-field-${field}`}>{field} → output</label>
          <input id={`dataset-field-${field}`} value={mapping[field] ?? field} disabled={busy}
            onChange={e => update(selected, { ...mapping, [field]: e.target.value })} />
        </div>)}
      </details>
      <div className="field">
        <label htmlFor="dataset-limit">Record limit (0 = all)</label>
        <input id="dataset-limit" type="number" min="0" step="1" value={config.n ?? 0}
          onChange={e => onChange({ ...config, n: Number(e.target.value) })} />
        <p className="muted small">{config.input_mode === 'reference' ? 'All selected rows travel together as one reference list. This Input does not multiply the number of runs.' : 'Single Run reads one record. Batch Run processes the selected records, including the final partial batch.'}</p>
      </div>
    </>}
  </section>;
}
