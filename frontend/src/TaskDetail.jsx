import React, { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { api } from './api.js';
import RunResultPage from './features/execution/RunResultPage.jsx';

export default function TaskDetail({ graphId, onBack, onEdit, onRun }) {
  const [graph,setGraph]=useState(null);
  const [plan,setPlan]=useState(null);
  const [runs,setRuns]=useState([]);
  const [error,setError]=useState('');
  const [planError,setPlanError]=useState('');
  const [revision,setRevision]=useState(0);
  const [selectedRun,setSelectedRun]=useState(null);
  const [sequence,setSequence]=useState(null);
  const overviewRef=useRef(null);
  const overviewPosition=useRef(0);
  const returnRun=useRef(null);
  useLayoutEffect(()=>{
    if(!selectedRun&&overviewRef.current){
      overviewRef.current.scrollTop=overviewPosition.current;
      const trigger=[...overviewRef.current.querySelectorAll('[data-run-id]')].find(el=>el.dataset.runId===returnRun.current);
      trigger?.focus({preventScroll:true});
    }
  },[selectedRun]);
  useEffect(()=>{
    let active=true;
    setError('');setPlanError('');setGraph(null);setPlan(null);
    Promise.all([api.getGraph(graphId),api.listResultRuns(graphId)]).then(([g,r])=>{
      if(active){setGraph(g);setRuns(r);}
    }).catch(e=>{if(active)setError(e.message);});
    api.runPlan(graphId).then(p=>{if(active)setPlan(p);}).catch(e=>{
      if(active)setPlanError(typeof e.body?.detail==='string'?e.body.detail:'This task needs configuration before it can run.');
    });
    return ()=>{active=false;};
  },[graphId,revision]);
  useEffect(()=>{
    if(!runs.some(r=>r.status==='running'))return;
    let active=true;
    const timer=setInterval(()=>{api.listResultRuns(graphId).then(r=>{if(active)setRuns(r);}).catch(e=>{if(active)setError(e.message);});},3000);
    return ()=>{active=false;clearInterval(timer);};
  },[graphId,runs]);
  function inspect(run){
    overviewPosition.current=overviewRef.current?.scrollTop||0;
    returnRun.current=run.run_id;
    setSelectedRun(run);
  }
  // The trajectory fields the workflow's Input declares, if any: a DataLoader
  // says so itself, any other type through its schema.
  useEffect(()=>{
    setSequence(null);
    const input=(graph?.tasks||[]).find(n=>n.kind==='source'&&n.enabled!==false);
    if(!input?.source?.type)return undefined;
    if(input.source.type==='dataloader'){if(input.source.group_by)setSequence({group:input.source.group_by,order:input.source.order_by||null});return undefined;}
    let active=true;
    api.listSourceTypes?.().then(r=>{const found=(r.source_types||[]).find(s=>s.type===input.source.type);if(active&&found?.sequence)setSequence(found.sequence);}).catch(()=>{});
    return ()=>{active=false;};
  },[graph]);
  const nodes=graph?.tasks||[];
  const tools=[...new Set(nodes.flatMap(n=>n.tool_names||[]))];
  const skills=[...new Set(nodes.flatMap(n=>n.skill_names||[]))];
  if(selectedRun)return <RunResultPage graphId={graphId} sourceNames={(graph?.tasks || []).filter(node => node.kind === "source").map(node => node.name)} sequence={sequence} run={selectedRun} runs={runs} onSelectRun={setSelectedRun} taskName={graph?.name} onBack={()=>setSelectedRun(null)}/>;
  return <main ref={overviewRef} className="task-detail project-main">
    <button onClick={onBack}>← Tasks</button>
    {error&&<div role="alert" className="platform-error">{error} <button onClick={()=>setRevision(v=>v+1)}>Retry</button></div>}
    {!graph&&!error&&<p role="status">Loading task…</p>}
    {graph&&<>
      <header className="project-heading"><div><div className="project-eyebrow">TASK OVERVIEW</div><h1>{graph.name}</h1><p>{graph.goal||'No goal specified yet. Define one in the workflow editor.'}</p></div><div className="form-actions"><button onClick={onEdit}>Edit workflow</button><button className="platform-primary" disabled={!plan} onClick={onRun}>Prepare run</button></div></header>
      {planError&&<div role="alert" className="platform-error">{planError} <button onClick={onEdit}>Configure task</button></div>}
      <div className="task-detail-grid">
        <section className="project-task-card"><h2>Inputs</h2><p>Provided when you run this task.</p>{(plan?.inputs||[]).map(i=><div className="task-field" key={i.name}><strong>{i.description||i.name}</strong><small>{i.name} · {i.type} · {i.required?'Required':'Optional'}</small></div>)}{plan&&!plan.inputs?.length&&<p>No manual inputs required.</p>}</section>
        <section className="project-task-card"><h2>Execution</h2><div className="task-field"><strong>{nodes.length} workflow steps</strong><small>Model: platform configuration; review before running.</small></div><div className="task-field"><strong>Tools</strong><small>{tools.join(', ')||'None configured'}</small></div><div className="task-field"><strong>Skills</strong><small>{skills.join(', ')||'None configured'}</small></div><p>Memory behavior is configured per step in the editor.</p></section>
      </div>
      <section className="project-task-section"><div className="task-section-heading"><h2>Recent runs</h2><button onClick={()=>setRevision(v=>v+1)}>Refresh</button></div>{!runs.length?<div className="project-empty"><h3>No runs yet</h3><p>Prepare a run to review inputs and settings. Nothing runs until you confirm.</p></div>:<div className="task-run-list">{runs.map(r=><button data-run-id={r.run_id} key={r.run_id} onClick={()=>inspect(r)}><span>{r.created_at?new Date(r.created_at).toLocaleString():r.run_id}</span><strong>{r.status}</strong><span>View result →</span></button>)}</div>}</section>
    </>}
  </main>;
}
