import NumberInput from '../../components/NumberInput.jsx';
import React, {useEffect,useRef,useState} from 'react';
import {api} from '../../api.js';
import PythonDatasetEditor from '../data/PythonDatasetEditor.jsx';
import DatasetInterface from '../data/DatasetInterface.jsx';
import JsonView from '../../components/JsonView.jsx';

export const EVALUATOR_EXAMPLE = `class CompletionEvaluator:
    def __init__(self, minimum_success_rate):
        self.minimum_success_rate = minimum_success_rate

    def evaluate(self, records):
        total = len(records)
        succeeded = sum(record.get("status") == "success" for record in records)
        rate = succeeded / total if total else None
        return {
            "metrics": {"completion_rate": rate},
            "coverage": {"unit": "executions", "total": total, "scored": total, "unscored": 0},
            "details": {"meets_threshold": rate is not None and rate >= self.minimum_success_rate},
        }


def build_evaluator(minimum_success_rate: float = 1.0):
    return CompletionEvaluator(minimum_success_rate)
`;

export default function EvaluatorInspector({node,onUpdate,onRename,getGraph}) {
 const cfg=node.data.evaluator || {};
 const graphId=getGraph?.()?.id;
 const [resources,setResources]=useState([]),[saved,setSaved]=useState([]),[selection,setSelection]=useState('');
 const [schema,setSchema]=useState(null),[report,setReport]=useState(null),[busy,setBusy]=useState(false),[error,setError]=useState('');
 const [reload,setReload]=useState(0);
 const [codeDirty,setCodeDirty]=useState(false);
 const [codeOpen,setCodeOpen]=useState(!cfg.output_schema?.length);
 const version=useRef(0);
 const python=cfg.type==='python';
 useEffect(()=>{api.listDataResources().then(r=>setResources(r.resources)).catch(()=>{});},[]);
 useEffect(()=>{
   let current=true;setSaved([]);setSelection('');
   if(graphId)Promise.all([api.listBatches(graphId),api.listRuns(graphId)]).then(([batches,runs])=>{
     if(!current)return;
     const done=r=>!['running','pending','queued','cancelling'].includes(r.status);
     const rows=[...(Array.isArray(batches)?batches:batches.batches || []).filter(done).map(r=>({id:`batch:${r.batch_id}`,label:`Batch ${r.batch_id} · ${r.total ?? '?'} runs · ${r.status}`})),...(Array.isArray(runs)?runs:runs.runs || []).filter(done).map(r=>({id:`run:${r.run_id}`,label:`Run ${r.run_id} · ${r.status}`}))];
     setSaved(rows);setSelection(rows[0]?.id || '');
   }).catch(e=>{if(current)setError(e.body?.detail || e.message);});
   return()=>{current=false;version.current+=1;};
 },[graphId,reload]);
 const update=patch=>{version.current+=1;setReport(null);if('code' in patch)setSchema(null);onUpdate(node.id,{evaluator:{...cfg,...patch,output_schema:null,...('code' in patch?{metric:''}:{})}});};
 const preview=async()=>{
   const token=++version.current;setBusy(true);setError('');
   try {
     const [kind,id]=selection.split(':');
     const graph=structuredClone(getGraph());
     // Use the visible config even if the graph callback trails a render.
     graph.tasks=graph.tasks.map(t=>t.name===node.id?{...t,evaluator:cfg}:t);
     const result=await api.previewEvaluatorCode(graphId,{graph,evaluator:node.id,[kind==='batch'?'batch_id':'run_id']:id});
     if(token!==version.current)return;
     setReport(result);onUpdate(node.id,{evaluator:{...cfg,input_schema:schema,output_schema:result.metrics,metric:result.report.objective.metric}});
   } catch(e){if(token===version.current)setError(e.body?.detail || e.message);}finally{setBusy(false);}
 };
 return <aside className="inspector"><h3>Evaluator: {node.id}</h3>
 <div className="field"><label>Name</label><input value={node.data.editName ?? node.id} onChange={e=>onUpdate(node.id,{editName:e.target.value})} onBlur={()=>onRename(node.id,node.data.editName ?? node.id)}/></div>
 {python ? <><details open={codeOpen} onToggle={e=>setCodeOpen(e.currentTarget.open)}><summary>Evaluation code</summary><PythonDatasetEditor key={node.id} onDirtyChange={setCodeDirty} kind="Evaluator" code={cfg.code} onChange={code=>update({code})} disabled={busy} example={EVALUATOR_EXAMPLE}/></details>
 <DatasetInterface title="Evaluator" code={cfg.code} values={cfg.config || {}} onChange={config=>update({config})} onSchema={setSchema} disabled={busy} inspectCode={api.inspectEvaluatorCode} providedText="The selected saved results supply records automatically. All node outputs are included. Fill the additional parameters detected from your code."/></>
 : <div><p className="muted small">Existing evaluator: {cfg.tool || cfg.type || 'exact_match'}. Its saved configuration still works.</p><button onClick={()=>onUpdate(node.id,{evaluator:{type:'python',timing:cfg.timing || 'batch',direction:cfg.direction || 'maximize',code:EVALUATOR_EXAMPLE,config:{},labels:cfg.labels}})}>Replace with Python evaluator</button></div>}
 <div className="field"><label htmlFor="eval-timing">Run evaluation</label><select id="eval-timing" value={cfg.timing || 'run'} onChange={e=>update({timing:e.target.value})}><option value="node">When connected outputs are ready</option><option value="run">After each workflow run</option><option value="batch">After the entire batch</option></select></div>
 {python && <div className="field"><label htmlFor="eval-timeout">Time limit (seconds)</label><NumberInput id="eval-timeout" type="number" min={10} max={3600} step={1} value={cfg.timeout ?? 120} onChange={e=>update({timeout:Number(e.target.value)})}/><small className="muted">How long your code may run on one evaluation before it is stopped (10–3600).</small></div>}
 <details><summary>Separate labels (optional)</summary><p className="muted small">Labels are passed as config.label_records (or a label_records parameter). Code decides how to match them.</p><div className="field"><label htmlFor="eval-label-resource">Label resource</label><select id="eval-label-resource" value={cfg.labels?.resource_id || ''} onChange={e=>update({labels:e.target.value?{resource_id:e.target.value,loader:'auto'}:null})}><option value="">None</option>{resources.map(r=><option key={r.id} value={r.id}>{r.name}</option>)}</select></div></details>
 {python && <><div className="field"><label htmlFor="eval-saved">Test with saved results</label><select id="eval-saved" value={selection} onChange={e=>{version.current+=1;setSelection(e.target.value);setReport(null);}}><option value="">Select a completed run or batch</option>{saved.map(r=><option key={r.id} value={r.id}>{r.label}</option>)}</select></div>
 <button disabled={busy || !graphId} onClick={()=>setReload(n=>n+1)}>Refresh saved results</button>
 <button disabled={busy || codeDirty || !selection || !schema || !graphId} onClick={preview}>{busy?'Evaluating…':'Run evaluator and detect metrics'}</button>
 <p className="muted small">Runs your code on saved results and detects all returned metrics. Choose one metric below as the Evolve objective. Workflow agents are not rerun.</p>
 <h4>Output metrics</h4>
 {(report?.metrics || cfg.output_schema)?.length ? <><table><thead><tr><th>Metric</th><th>Type</th><th>Example</th></tr></thead><tbody>{(report?.metrics || cfg.output_schema).map(f=><tr key={f.name}><td>{f.name}</td><td>{f.type}{f.nullable?' / null':''}</td><td>{String(f.sample)}</td></tr>)}</tbody></table>
 <div className="field"><label htmlFor="eval-metric">Evolve objective</label><select id="eval-metric" value={cfg.metric || ''} onChange={e=>onUpdate(node.id,{evaluator:{...cfg,metric:e.target.value}})}>{(report?.metrics || cfg.output_schema).map(f=><option key={f.name} value={f.name}>{f.name}</option>)}</select></div></> : <p className="muted small">Preview to identify metric names, values and details before using this evaluator in Evolve.</p>}
 {report && <details><summary>Full evaluation report · {report.execution_count} executions</summary><JsonView value={report.report}/></details>}</>}
 <div className="field"><label htmlFor="eval-direction">Optimize direction</label><select id="eval-direction" value={cfg.direction || 'maximize'} onChange={e=>onUpdate(node.id,{evaluator:{...cfg,direction:e.target.value}})}><option value="maximize">Maximize</option><option value="minimize">Minimize</option></select></div>
 {error && <p role="alert">{String(error)}</p>}
 <label><input type="checkbox" checked={node.data.enabled!==false} onChange={e=>onUpdate(node.id,{enabled:e.target.checked})}/> Enabled</label>
 </aside>;
}
