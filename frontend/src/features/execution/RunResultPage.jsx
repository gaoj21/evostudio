import React, { useEffect, useRef, useState } from 'react';
import { Group, Panel, Separator, useDefaultLayout } from 'react-resizable-panels';
import { useLayoutMode } from '../../useLayoutMode.js';
import { api } from '../../api.js';
import ResultChat from '../chat/ResultChat.jsx';
import JsonView from '../../components/JsonView.jsx';

const RESULT_PANEL_IDS = ['result-detail', 'result-chat'];

export default function RunResultPage({ graphId, sourceNames = [], run, runs = [], onSelectRun, taskName, onBack }) {
  const layout = useLayoutMode();
  const { defaultLayout, onLayoutChanged } = useDefaultLayout({ id: `results-${layout}`, panelIds: RESULT_PANEL_IDS });
  const [result, setResult] = useState(null);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const heading = useRef(null);
  const content = useRef(null);
  const [batch, setBatch] = useState('all');
  const [query, setQuery] = useState('');
  const batches = [...new Set(runs.map(item => item.batch_id).filter(Boolean))];
  const label = item => {
    const source = item.nodes?.find(node => node.name === 'feed')?.output || item.input_summary || item.inputs || {};
    return [source.company || source.name, source.as_of].filter(Boolean).join(' · ') || item.run_id;
  };
  const visibleRuns = runs.filter(item => (batch === 'all' || (batch === 'single' ? !item.batch_id : item.batch_id === batch)) && `${label(item)} ${item.run_id} ${item.status}`.toLowerCase().includes(query.toLowerCase()));


  useEffect(() => { heading.current?.focus(); }, []);
  useEffect(() => {
    let active = true;
    let timer;
    setError('');
    setResult(null);
    if (content.current) content.current.scrollTop = 0;
    async function load() {
      try {
        const value = await api.getRun(run.run_id);
        if (!active) return;
        setResult(value);
        if (['pending', 'queued', 'running', 'stopping'].includes(value.status)) {
          timer = setTimeout(load, 3000);
        }
      } catch (e) {
        if (active) setError(e.message || 'Unable to load this result.');
      }
    }
    load();
    return () => { active = false; clearTimeout(timer); };
  }, [run.run_id, revision]);

  const status = result?.status || run.status;
  const createdAt = result?.created_at || run.created_at;
  return <main className="task-detail project-main run-result-page">
    <nav className="run-result-nav" aria-label="Result navigation">
      <button onClick={onBack}>← Back to task</button>
      <span>{taskName}</span>
    </nav>
    <div className="run-result-layout">
      <aside className="run-result-list" aria-label="Run records">
        <h2>Run records <small>({visibleRuns.length} / {runs.length})</small></h2>
        <input aria-label="Search run records" placeholder="Search records…" value={query} onChange={e => setQuery(e.target.value)} />
        <select aria-label="Filter batch" value={batch} onChange={e => setBatch(e.target.value)}>
          <option value="all">All runs</option><option value="single">Individual runs</option>
          {batches.map(id => <option key={id} value={id}>Batch {id} ({runs.filter(item => item.batch_id === id).length})</option>)}
        </select>
        <div className="run-result-records">
          {visibleRuns.map(item => <button key={item.run_id} aria-current={item.run_id === run.run_id ? 'true' : undefined} onClick={() => onSelectRun?.(item)}>
            <strong>{label(item)}</strong><span>{item.status} · {item.created_at ? new Date(item.created_at).toLocaleString() : item.run_id}</span>
          </button>)}
          {!visibleRuns.length && <p>No matching records.</p>}
        </div>
      </aside>
      <Group className="result-workspace" orientation={layout === 'phone' ? 'vertical' : 'horizontal'} defaultLayout={defaultLayout} onLayoutChanged={onLayoutChanged}>
      <Panel id="result-detail" defaultSize="65" minSize="25">
      <article className="run-result-detail" ref={content} key={run.run_id}>
    <header className="project-heading">
      <div>
        <div className="project-eyebrow">RUN RESULT</div>
        <h1 ref={heading} tabIndex={-1}>Run result</h1><p>{label(result || run)}</p>
        <div className="run-result-meta"><strong>{status}</strong>{createdAt && <time>{new Date(createdAt).toLocaleString()}</time>}<span>{run.run_id}</span></div>
      </div>
    </header>
    {error && <div role="alert" className="platform-error">{error} <button onClick={() => setRevision(v => v + 1)}>Retry</button></div>}
    {!result && !error && <p role="status">Loading result…</p>}
    {result && <>
      {result.error && <div role="alert" className="platform-error">{typeof result.error === 'string' ? result.error : JSON.stringify(result.error)}</div>}
      <details className="run-result-content result-input-data" aria-label="Input data" open><summary>Input data</summary>
        {Object.keys(result.inputs || {}).length > 0 ? <JsonView value={result.inputs} /> : <p>No manually supplied inputs. Source data is shown below when saved by the workflow.</p>}
        {(result.nodes || []).filter(node => sourceNames.includes(node.name) || node.name === 'feed' || node.name.startsWith('source_')).map(node => <details key={node.name}><summary>{node.name} · Source data</summary><JsonView value={node.output ?? 'No source data saved.'} /></details>)}
      </details>
      <details className="run-result-content" aria-label="Run result" open>
        <summary>Final output</summary>
        <JsonView key={run.run_id} value={result.result ?? { message: ['pending', 'queued', 'running', 'stopping'].includes(status) ? 'This run is still in progress. Results will appear here automatically.' : 'No final result available.' }} />
      </details>
      <details className="run-result-content" aria-label="Step outputs" open><summary>Step outputs ({result.nodes?.length || 0})</summary>
        {(result.nodes || []).map((node, index) => <details key={`${node.name}-${index}`}><summary>{node.name} · {node.status}</summary>{node.error && <p role="alert">{String(node.error)}</p>}<JsonView value={node.output ?? 'No output available.'} /></details>)}
      </details>
    </>}
      </article>
      </Panel>
      <Separator className="resize-handle result-chat-resize" aria-label="Resize result and chat panels" />
      <Panel id="result-chat" defaultSize="35" minSize="20">
      <ResultChat graphId={graphId} run={run} />
      </Panel>
      </Group>
    </div>
  </main>;
}
