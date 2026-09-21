import NumberInput from '../../components/NumberInput.jsx';
import React, { useEffect, useRef, useState } from 'react';
import { api } from '../../api.js';
import JsonView from '../../components/JsonView.jsx';
import DatasetInterface, {DatasetOutputs} from './DatasetInterface.jsx';
import PythonDatasetEditor, {DATASET_EXAMPLE} from './PythonDatasetEditor.jsx';

export default function DataLoaderInput({ config, onChange, getGraph, nodeId }) {
  const [resources, setResources] = useState([]);
  const [preview, setPreview] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [codeDirty,setCodeDirty] = useState(false);
  const [inputSchema,setInputSchema] = useState(null);
  const [codeOpen, setCodeOpen] = useState(!config.output_schema?.length);
  const resultRef = useRef(null);
  const legacy = config.loader !== 'python';
  const files = useRef(null), folder = useRef(null), version = useRef(0);
  const update = patch => {
    version.current += 1; setPreview(null); setError('');
    if ('code' in patch) setInputSchema(null);
    const runtimeOnly = Object.keys(patch).every(key => ['read_batch_size','batch_timeout','n','offset','cache'].includes(key));
    const declared = config.output_schema_mode === 'declared';
    const changesShape = ['code','input_mode','reference_field','field_mapping','transform_tool'].some(key => key in patch);
    const clearOutputs = changesShape || (!runtimeOnly && !declared);
    onChange({...config, ...patch, preview_snapshot:null,
      ...(clearOutputs ? {output_schema:null, output_schema_mode:null} : {})}, clearOutputs ? [] : undefined);
  };
  useEffect(() => {
    if (preview || error) resultRef.current?.scrollIntoView?.({block:'nearest'});
  }, [preview, error]);
  useEffect(() => {
    api.listDataResources().then(r => setResources(r.resources)).catch(e => setError(e.body?.detail || e.message));
  }, []);
  // Schemas by type, for the `local` flag; null until known.
  const [sourceTypes, setSourceTypes] = useState(null);
  const wrappedType = config.loader === 'source' ? config.source_config?.type : null;
  useEffect(() => {
    if (!wrappedType || !api.listSourceTypes) return undefined;
    let active = true;
    api.listSourceTypes().then(r => { if (active) setSourceTypes(Object.fromEntries((r.source_types || []).map(t => [t.type, t]))); }).catch(() => {});
    return () => { active = false; };
  }, [wrappedType]);
  async function selectResource(resourceId) {
    const graphId=getGraph?.()?.id;
    setError('');
    if (!resourceId || !graphId) {update({resource_id:resourceId});return;}
    setBusy(true);
    try {
      await api.attachDataResource(resourceId, graphId);
      update({resource_id:resourceId});
    } catch(e) {setError(e.body?.detail || e.message);}
    finally {setBusy(false);}
  }
  async function upload(selected) {
    if (!selected?.length) return;
    setBusy(true); setError('');
    try {
      const item = await api.uploadDataResource(selected, getGraph?.()?.id);
      setResources(r => [...r, item]); update({resource_id: item.id});
    } catch(e) { setError(e.body?.detail || e.message); }
    finally { setBusy(false); }
  }
  async function sampleOutputs() {
    const token = ++version.current;
    setBusy(true); setError('');
    try {
      const result = await api.previewDataLoader({...config,preview_mode:'sample',...(getGraph ? {_graph:getGraph(),_node:nodeId} : {})});
      if (token !== version.current) return;
      if (!Array.isArray(result.fields) || !result.fields.length) throw new Error('No output fields were found. Check that your Dataset returns non-empty records for the selected inputs.');
      setPreview(result); onChange({...config, preview_snapshot: result.snapshot, input_schema:inputSchema, output_schema:result.fields, output_schema_mode:result.preview_mode}, result.fields);
    } catch(e) { if (token === version.current) setError(e.body?.detail || e.message); }
    finally { setBusy(false); }
  }
  const field = (key, title, placeholder = '') => <div className="field"><label htmlFor={`loader-${key}`}>{title}</label><input id={`loader-${key}`} value={config[key] || ''} placeholder={placeholder} onChange={e => update({[key]: e.target.value})} /></div>;
  // Local Input types (files, project data) run inline; the rest call an API
  // and are collected first. The schema says which is which.
  const apiSource = config.loader === 'source' && !!config.source_config?.type && sourceTypes !== null && !sourceTypes[config.source_config.type]?.local;
  return <section aria-label="DataLoader">
    {apiSource && <p role="status" className="muted small" data-testid="api-source-note">This Input reads an API source. A batch run collects it first, in the background, over the whole configured range; the collected records are saved as a data resource. To preprocess them in code, choose that resource in a PyTorch Dataset Input.</p>}
    {config.loader !== 'source' && <><div className="field"><label htmlFor="loader-resource">Data resource</label><select id="loader-resource" value={config.resource_id || ''} disabled={busy} onChange={e => selectResource(e.target.value)}><option value="">Select uploaded files</option>{resources.map(r => <option key={r.id} value={r.id}>{r.name} · {r.files.length} files</option>)}</select></div>
    <input hidden ref={files} type="file" multiple onChange={e => {upload(e.target.files); e.target.value='';}} />
    <input hidden ref={folder} type="file" webkitdirectory="" multiple onChange={e => {upload(e.target.files); e.target.value='';}} />
    <div className="input-actions"><button disabled={busy} onClick={() => files.current.click()}>Upload files</button><button disabled={busy} onClick={() => folder.current.click()}>Upload folder</button></div></>}
    {resources.find(r=>r.id===config.resource_id)?.root && <div className="field"><label>Dataset folder path</label><input readOnly value={resources.find(r=>r.id===config.resource_id).root}/><button type="button" onClick={async()=>{try{await navigator.clipboard.writeText(resources.find(r=>r.id===config.resource_id).root);}catch{setError('Copy the path from the field above.');}}}>Copy path</button><small className="muted">Uploaded files are visible under Workspace → datasets.</small></div>}
    <h4>PyTorch Dataset</h4>
    {legacy ? <div role="status"><p className="muted small">This saved Input uses an older reader. It will keep working until you replace it with your Dataset code. Replacement clears old reader, preprocessing and output-field settings; include that processing in your code.</p><button disabled={busy} onClick={()=>{
      const {reader_tool,transform_tool,transform_scope,field_mapping,record_path,source_config,...kept}=config;
      version.current += 1; setPreview(null);
      onChange({...kept,loader:'python',code:DATASET_EXAMPLE},[]);
    }}>Replace with PyTorch Dataset</button></div> : <details open={codeOpen} onToggle={e=>setCodeOpen(e.currentTarget.open)}><summary>Dataset code</summary><PythonDatasetEditor key={nodeId} onDirtyChange={setCodeDirty} code={config.code} onChange={code=>update({code})} disabled={busy}/></details>}
    <p className="muted small">Read and preprocess in build_dataset or Dataset.__getitem__. Studio handles batching and keeps the final partial batch.</p>
    <div className="field"><label htmlFor="loader-read_batch_size">Batch size</label><NumberInput id="loader-read_batch_size" type="number" min={1} max={1024} value={config.read_batch_size ?? 100} onChange={e=>update({read_batch_size:Number(e.target.value)})}/><small className="muted">Shared by data loading, workflow batches and native API batching.</small></div>
    <div className="field"><label htmlFor="loader-n">Sample count (0 = all records)</label><NumberInput id="loader-n" min={0} step={1} value={config.n ?? 0} onChange={e=>update({n:Number(e.target.value)})}/><small className="muted">Limits records at the reader before materialization. Dataset constructors must still use lazy loading for large files.</small></div>
    <div className="field"><label htmlFor="loader-role">How to use the data</label><select id="loader-role" value={config.input_mode || 'records'} onChange={e => update({input_mode:e.target.value})}><option value="records">Per-record input</option><option value="reference">Shared reference</option></select></div>
    {config.input_mode === 'reference' && field('reference_field', 'Reference output name', 'reference_data')}
    {!legacy && <DatasetInterface code={config.code} values={config.reader_config || {}} onChange={reader_config=>update({reader_config})} onSchema={setInputSchema} disabled={busy}/>}
    <details className="input-disclosure"><summary>Advanced settings</summary>
      <label><input type="checkbox" checked={config.cache !== false} onChange={e=>update({cache:e.target.checked})}/> Reuse prepared data until code, configuration or files change</label>
      <div className="field"><label htmlFor="loader-batch_timeout">Seconds allowed per batch</label><NumberInput id="loader-batch_timeout" min={10} max={3600} step={1} value={config.batch_timeout ?? 120} onChange={e=>update({batch_timeout:Number(e.target.value)})}/><small className="muted">How long your Dataset may take to produce one batch before the run stops with an error (10–3600). Raise it when items do heavy work such as calling an API.</small></div>
      {field('file_pattern','File pattern','*')}
      {['offset'].map(key=><div className="field" key={key}><label htmlFor={`loader-${key}`}>{key==='offset'?'Skip records':'Record limit (0 = all)'}</label><NumberInput id={`loader-${key}`} type="number" min={0} value={config[key] ?? 0} onChange={e=>update({[key]:Number(e.target.value)})}/></div>)}
      {field('group_by','Sequential group field','Optional')}{field('order_by','Order within group','Optional')}
    </details>
    {!legacy && <>
      <button type="button" className="primary" disabled={busy || codeDirty || !inputSchema || !config.resource_id} onClick={sampleOutputs}>{busy ? 'Sampling…' : 'Sample 1 record (detect outputs)'}</button>
      <p className="muted small">Runs your Dataset to sample 1 record, then applies their field names and types to the canvas output ports. Execution has a 30-second limit.</p>
    </>}
    <div ref={resultRef} aria-live="polite">
    {error && <p role="alert">{String(error)}</p>}
    {preview && <><p role="status">{preview.fields.length} output field(s) applied to this node. {' '}{preview.preview_mode==='declared' ? 'Output schema read from code. Dataset was not loaded; runtime values have not been verified.' : preview.preview_mode==='sample' ? `Interface inferred from up to ${preview.sample_limit} sampled records. This is not a full dataset scan or record count.` : `${preview.raw_records} raw → ${preview.output_records} output records`}</p>{preview.preview?.length > 0 && <JsonView value={preview.preview} />}</>}
    </div>
    <DatasetOutputs fields={preview?.fields || config.output_schema} mode={preview?.preview_mode || config.output_schema_mode}/>
    {config.resource_id && <details className="input-disclosure"><summary>Manage resource</summary><button onClick={() => update({resource_id:''})}>Detach from this Input</button><button disabled={busy} onClick={async () => {if (!window.confirm('Delete these uploaded files? Saved workflow references must be detached first.')) return; try {await api.deleteDataResource(config.resource_id);setResources(r => r.filter(x => x.id !== config.resource_id));update({resource_id:''});} catch(e){setError(e.body?.detail || e.message);}}}>Delete resource</button></details>}
  </section>;
}
