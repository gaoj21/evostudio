import TokenUsage from '../../components/TokenUsage.jsx';
import React, { useEffect, useRef, useState } from 'react';
import { Group, Panel, Separator, useDefaultLayout } from 'react-resizable-panels';
import { useLayoutMode } from '../../useLayoutMode.js';
import { api } from '../../api.js';
import ResultChat from '../chat/ResultChat.jsx';
import EvaluatorReports from '../evaluation/EvaluatorReports.jsx';
import JsonView from '../../components/JsonView.jsx';

const RESULT_PANEL_IDS = ['result-detail', 'result-chat'];

const isSourceNode = (node, sourceNames) => node.kind === 'source' || sourceNames.includes(node.name) || String(node.name || '').startsWith('source_');
const shortScalar = value => (typeof value === 'string' && value.trim() && value.length <= 60) || typeof value === 'number' || typeof value === 'boolean';

// What names a run in the list: the record its Input produced, read by the
// trajectory fields the Input declares, else its first two short values.
export function runLabel(item, { sourceNames = [], sequence = null } = {}) {
  const fromSource = (item.nodes || []).find(node => isSourceNode(node, sourceNames)
    && node.output && typeof node.output === 'object' && !Array.isArray(node.output))?.output;
  const record = fromSource || item.input_summary || item.inputs || {};
  const pick = keys => keys.map(key => record[key]).filter(shortScalar);
  const declared = sequence?.group ? pick([sequence.group, sequence.order].filter(Boolean)) : [];
  const parts = declared.length ? declared : pick(Object.keys(record)).slice(0, 2);
  return parts.join(' · ') || item.run_id;
}

// Runs are listed a page at a time; search and the batch filter run on the
// server, over every run of the task rather than only the ones loaded.
export const RUN_PAGE = 50;
export const asRunPage = value => Array.isArray(value) ? { runs: value, total: value.length, has_more: false } : value;
const SEARCH_DELAY = 300;
const filterKey = (batch, query) => `${batch}\n${query.trim()}`;
const pageParams = (batch, query, offset) => ({ limit: RUN_PAGE, offset, batch: batch === 'all' ? undefined : batch, q: query.trim() || undefined });

export default function RunResultPage({ graphId, sourceNames = [], sequence = null, run, runs = [], total, hasMore = false, batches = [], onSelectRun, taskName, onBack }) {
  const layout = useLayoutMode();
  const { defaultLayout, onLayoutChanged } = useDefaultLayout({ id: `results-${layout}`, panelIds: RESULT_PANEL_IDS });
  const [result, setResult] = useState(null);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const heading = useRef(null);
  const content = useRef(null);
  const [batch, setBatch] = useState('all');
  const [query, setQuery] = useState('');
  // The listed runs and the filter they were fetched for; until a filter is
  // applied or more are loaded, the task page's newest page (which it polls).
  const [list, setList] = useState(() => ({ runs, total: total ?? runs.length, hasMore, key: filterKey('all', ''), own: false }));
  const [listLoading, setListLoading] = useState(false);
  const [listError, setListError] = useState('');
  const applied = useRef({ batch: 'all', query: '' });
  const label = item => runLabel(item, { sourceNames, sequence });

  useEffect(() => {
    setList(current => current.own ? current : { ...current, runs, total: total ?? runs.length, hasMore });
  }, [runs, total, hasMore]);
  useEffect(() => {
    const key = filterKey(batch, query);
    if (key === list.key) return undefined;
    let active = true;
    setListLoading(true);
    const timer = setTimeout(() => {
      applied.current = { batch, query };
      api.listResultRuns(graphId, pageParams(batch, query, 0)).then(value => {
        if (!active) return;
        const page = asRunPage(value);
        setList({ runs: page.runs, total: page.total, hasMore: page.has_more, key, own: key !== filterKey('all', '') });
        setListError('');
      }).catch(e => { if (active) setListError(e.message || 'Unable to load run records.'); })
        .finally(() => { if (active) setListLoading(false); });
    }, query.trim() ? SEARCH_DELAY : 0);
    return () => { active = false; clearTimeout(timer); setListLoading(false); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graphId, batch, query]);
  function loadMore() {
    const { batch: shownBatch, query: shownQuery } = applied.current;
    const key = list.key;
    setListLoading(true);
    api.listResultRuns(graphId, pageParams(shownBatch, shownQuery, list.runs.length)).then(value => {
      const page = asRunPage(value);
      setList(current => {
        if (current.key !== key) return current;
        const seen = new Set(current.runs.map(item => item.run_id));
        return { runs: [...current.runs, ...page.runs.filter(item => !seen.has(item.run_id))], total: page.total, hasMore: page.has_more, key, own: true };
      });
      setListError('');
    }).catch(e => setListError(e.message || 'Unable to load run records.'))
      .finally(() => setListLoading(false));
  }


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
        <h2>Run records <small>({list.runs.length} / {list.total})</small></h2>
        <input aria-label="Search run records" placeholder="Search records…" value={query} onChange={e => setQuery(e.target.value)} />
        <select aria-label="Filter batch" value={batch} onChange={e => setBatch(e.target.value)}>
          <option value="all">All runs</option><option value="single">Individual runs</option>
          {batches.map(item => <option key={item.batch_id} value={item.batch_id}>Batch {item.created_at ? new Date(item.created_at).toLocaleString() : item.batch_id} ({item.input_total ?? item.total ?? 0})</option>)}
        </select>
        <div className="run-result-records">
          {list.runs.map(item => <button key={item.run_id} aria-current={item.run_id === run.run_id ? 'true' : undefined} onClick={() => onSelectRun?.(item)}>
            <strong>{label(item)}</strong><span>{item.status} · {item.created_at ? new Date(item.created_at).toLocaleString() : item.run_id}</span>
          </button>)}
          {listError && <p role="alert">{listError}</p>}
          {!list.runs.length && !listLoading && !listError && <p>No matching records.</p>}
          {list.hasMore && <button className="run-result-more" disabled={listLoading} onClick={loadMore}>{listLoading ? 'Loading…' : 'Load more'}</button>}
          {listLoading && !list.hasMore && <p role="status">Loading…</p>}
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
      <TokenUsage usage={result.token_usage} running={['pending', 'queued', 'running', 'stopping'].includes(status)}/>
      {result.error && <div role="alert" className="platform-error">{typeof result.error === 'string' ? result.error : JSON.stringify(result.error)}</div>}
      <EvaluatorReports reports={result.evaluations} />
      <details className="run-result-content result-input-data" aria-label="Input data" open><summary>Input data</summary>
        {Object.keys(result.inputs || {}).length > 0 ? <JsonView value={result.inputs} /> : <p>No manually supplied inputs. Source data is shown below when saved by the workflow.</p>}
        {(result.nodes || []).filter(node => isSourceNode(node, sourceNames)).map(node => <details key={node.name}><summary>{node.name} · Source data</summary><JsonView value={node.output ?? 'No source data saved.'} /></details>)}
      </details>
      <details className="run-result-content" aria-label="Run result" open>
        <summary>Final output</summary>
        <JsonView key={run.run_id} value={result.result ?? { message: ['pending', 'queued', 'running', 'stopping'].includes(status) ? 'This run is still in progress. Results will appear here automatically.' : 'No final result available.' }} />
      </details>
      <details className="run-result-content" aria-label="Step outputs" open><summary>Step outputs ({result.nodes?.length || 0})</summary>
        {(result.nodes || []).map((node, index) => <details key={`${node.name}-${index}`}><summary>{node.name} · {node.status}</summary>{node.error && <p role="alert">{String(node.error)}</p>}<JsonView value={result.node_outputs?.[node.name] ?? node.output ?? 'No output available.'} /></details>)}
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
