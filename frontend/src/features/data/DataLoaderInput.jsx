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
  const legacy = config.loader !== 'python';
  const files = useRef(null), folder = useRef(null), version = useRef(0);
  const update = patch => { version.current += 1; setPreview(null); if ('code' in patch) setInputSchema(null); onChange({...config, ...patch, preview_snapshot:null, output_schema:null}, []); };
  useEffect(() => {
    api.listDataResources().then(r => setResources(r.resources)).catch(e => setError(e.body?.detail || e.message));
  }, []);
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
  async function inspect() {
    const token = ++version.current;
    setBusy(true); setError('');
    try {
      const result = await api.previewDataLoader({...config,...(getGraph ? {_graph:getGraph(),_node:nodeId} : {})});
      if (token !== version.current) return;
      setPreview(result); onChange({...config, preview_snapshot: result.snapshot, input_schema:inputSchema, output_schema:result.fields}, result.fields);
    } catch(e) { if (token === version.current) setError(e.body?.detail || e.message); }
    finally { setBusy(false); }
  }
  const field = (key, title, placeholder = '') => <div className="field"><label htmlFor={`loader-${key}`}>{title}</label><input id={`loader-${key}`} value={config[key] || ''} placeholder={placeholder} onChange={e => update({[key]: e.target.value})} /></div>;
  return <section aria-label="DataLoader">
    {config.loader !== 'source' && <><div className="field"><label htmlFor="loader-resource">1. Data resource</label><select id="loader-resource" value={config.resource_id || ''} disabled={busy} onChange={e => selectResource(e.target.value)}><option value="">Select uploaded files</option>{resources.map(r => <option key={r.id} value={r.id}>{r.name} · {r.files.length} files</option>)}</select></div>
    <input hidden ref={files} type="file" multiple onChange={e => {upload(e.target.files); e.target.value='';}} />
    <input hidden ref={folder} type="file" webkitdirectory="" multiple onChange={e => {upload(e.target.files); e.target.value='';}} />
    <div className="input-actions"><button disabled={busy} onClick={() => files.current.click()}>Upload files</button><button disabled={busy} onClick={() => folder.current.click()}>Upload folder</button></div></>}
    {resources.find(r=>r.id===config.resource_id)?.root && <div className="field"><label>Dataset folder path</label><input readOnly value={resources.find(r=>r.id===config.resource_id).root}/><button type="button" onClick={async()=>{try{await navigator.clipboard.writeText(resources.find(r=>r.id===config.resource_id).root);}catch{setError('Copy the path from the field above.');}}}>Copy path</button><small className="muted">Uploaded files are visible under Workspace → datasets.</small></div>}
    <h4>2. PyTorch Dataset</h4>
    {legacy ? <div role="status"><p className="muted small">This saved Input uses an older reader. It will keep working until you replace it with your Dataset code. Replacement clears old reader, preprocessing and output-field settings; include that processing in your code.</p><button disabled={busy} onClick={()=>{
      const {reader_tool,transform_tool,transform_scope,field_mapping,record_path,source_config,...kept}=config;
      version.current += 1; setPreview(null);
      onChange({...kept,loader:'python',code:DATASET_EXAMPLE},[]);
    }}>Replace with PyTorch Dataset</button></div> : <details open={!config.output_schema?.length}><summary>Dataset code</summary><PythonDatasetEditor key={nodeId} onDirtyChange={setCodeDirty} code={config.code} onChange={code=>update({code})} disabled={busy}/></details>}
    <p className="muted small">Read and preprocess in build_dataset or Dataset.__getitem__. Studio handles batching and keeps the final partial batch.</p>
    <div className="field"><label htmlFor="loader-read_batch_size">Batch size</label><input id="loader-read_batch_size" type="number" min={1} max={1024} value={config.read_batch_size ?? 100} onChange={e=>update({read_batch_size:Number(e.target.value)})}/><small className="muted">Shared by data loading, workflow batches and native API batching.</small></div>
    <div className="field"><label htmlFor="loader-role">How to use the data</label><select id="loader-role" value={config.input_mode || 'records'} onChange={e => update({input_mode:e.target.value})}><option value="records">Per-record input</option><option value="reference">Shared reference</option></select></div>
    {config.input_mode === 'reference' && field('reference_field', 'Reference output name', 'reference_data')}
    {!legacy && <DatasetInterface code={config.code} values={config.reader_config || {}} onChange={reader_config=>update({reader_config})} onSchema={setInputSchema} disabled={busy}/>}
    <details className="input-disclosure"><summary>Advanced settings</summary>
      <label><input type="checkbox" checked={config.cache !== false} onChange={e=>update({cache:e.target.checked})}/> Reuse prepared data until code, configuration or files change</label>
      {field('file_pattern','File pattern','*')}
      {['offset','n'].map(key=><div className="field" key={key}><label htmlFor={`loader-${key}`}>{key==='offset'?'Skip records':'Record limit (0 = all)'}</label><input id={`loader-${key}`} type="number" min={0} value={config[key] ?? 0} onChange={e=>update({[key]:Number(e.target.value)})}/></div>)}
      {field('group_by','Sequential group field','Optional')}{field('order_by','Order within group','Optional')}
    </details>
    <button className="primary" disabled={busy || codeDirty || (!legacy && !inputSchema) || (config.loader !== 'source' && !config.resource_id)} onClick={inspect}>{busy ? 'Preparing…' : '3. Preview and update output fields'}</button>
    <DatasetOutputs fields={preview?.fields || config.output_schema}/>
    {error && <p role="alert">{String(error)}</p>}
    {preview && <><p role="status">{preview.preview_mode==='declared' ? 'Output schema read from code. Dataset was not loaded; runtime values have not been verified.' : preview.preview_mode==='sample' ? `Interface inferred from up to ${preview.sample_limit} sampled records. This is not a full dataset scan or record count.` : `${preview.raw_records} raw → ${preview.output_records} output records`}</p><JsonView value={preview.preview} /></>}
    {config.resource_id && <details className="input-disclosure"><summary>Manage resource</summary><button onClick={() => {update({resource_id:''}); onChange({...config,resource_id:''},[]);}}>Detach from this Input</button><button disabled={busy} onClick={async () => {if (!window.confirm('Delete these uploaded files? Saved workflow references must be detached first.')) return; try {await api.deleteDataResource(config.resource_id);setResources(r => r.filter(x => x.id !== config.resource_id));onChange({...config,resource_id:''},[]);} catch(e){setError(e.body?.detail || e.message);}}}>Delete resource</button></details>}
  </section>;
}
