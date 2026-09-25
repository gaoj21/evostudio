import React, { useEffect, useState } from 'react';
import { api } from '../../api.js';
import { workflowEvaluation } from './EvaluatePanel.jsx';

// Scoring is always the workflow's evaluation code, from Evaluate: there are
// no built-in metrics, expected-answer fields or evaluators to pick.

export function NewTaskForm({ graphId, onStarted, onError, initialSource = 'saved_batch', initialMode = 'evaluate' }) {
  const [evaluation, setEvaluation] = useState(null);   // the workflow's evaluation code
  const evaluator = evaluation?.name || '';
  const [rounds, setRounds] = useState(1);
  const [heldOut, setHeldOut] = useState(30);          // % of entities kept for validation
  const [workers, setWorkers] = useState(4);           // records of a chunk replayed at once
  const [mode, setMode] = useState(initialMode);
  const evaluationOnly = mode === 'evaluate';
  const [source, setSource] = useState(initialSource);
  const savedSource = source === 'saved_batch' || source === 'saved_run';
  const [savedResults, setSavedResults] = useState([]);
  const [savedId, setSavedId] = useState('');
  const [selection, setSelection] = useState(null);
  const [selectionError, setSelectionError] = useState('');
  const [selectionLoading, setSelectionLoading] = useState(false);
  const [nodes, setNodes] = useState([]);          // LLM nodes of the graph
  const [chosen, setChosen] = useState(null);      // null = all
  const [shareLabels, setShareLabels] = useState(false);
  const [starting, setStarting] = useState(false);

  useEffect(() => {
    if (graphId) {
      api.getGraph(graphId)
        .then((g) => setNodes((g.tasks || []).filter((t) => !['source', 'tool'].includes(t.kind)).map((t) => t.name)))
        .catch(() => setNodes([]));
      // The objective is the workflow's evaluation code, written in Evaluate.
      api.graphEvaluators(graphId)
        .then((r) => setEvaluation(workflowEvaluation((Array.isArray(r) ? r : r.evaluators || []).filter((e) => e.enabled !== false))))
        .catch(() => setEvaluation(null));
    }
  }, [graphId]);

  useEffect(() => {
    let active = true;
    setSavedResults([]); setSavedId('');
    if (savedSource && graphId) {
      const request = source === 'saved_batch' ? api.listBatches(graphId) : api.listRuns(graphId);
      request.then(rows => { if (active) { const done = rows.filter(r => !['running', 'pending', 'queued'].includes(r.status)); setSavedResults(done); setSavedId((source === 'saved_batch' ? done[0]?.batch_id : done[0]?.run_id) || ''); } }).catch(() => { if (active) onError('Unable to load saved results.'); });
    }
    return () => { active = false; };
  }, [graphId, source]);

  useEffect(() => {
    let active = true;
    setSelection(null); setSelectionError('');
    if (!savedSource || !savedId) { setSelectionLoading(false); return () => { active = false; }; }
    setSelectionLoading(true);
    api.previewEvolveResults(graphId, {source, ...(evaluator ? {evaluator} : {}),
      [source === 'saved_batch' ? 'batch_id' : 'run_id']: savedId}).then(value => {
        if (active) setSelection(value);
      }).catch(err => { if (active) setSelectionError(err?.body?.detail || err.message); })
      .finally(() => { if (active) setSelectionLoading(false); });
    return () => { active = false; };
  }, [graphId, source, savedId, evaluator]);

  const picked = chosen === null ? nodes : chosen;
  const toggleNode = (name) => setChosen((current) => {
    const base = current === null ? nodes : current;
    return base.includes(name) ? base.filter((x) => x !== name) : [...base, name];
  });

  const start = async (e) => {
    e.preventDefault();
    if (!evaluationOnly && picked.length === 0) { onError('Choose at least one node to optimize.'); return; }
    setStarting(true);
    try {
      const res = source === 'canvas'
        ? await api.startEvolveResults(graphId,{source:'canvas',mode,evaluator,nodes:evaluationOnly?[]:picked,rounds:Number(rounds),share_labels:shareLabels,workers:Number(workers),...(evaluationOnly?{}:{val_fraction:Number(heldOut)/100})})
        : await api.startEvolveResults(graphId, {source, mode, evaluator, nodes: evaluationOnly ? [] : picked, share_labels: shareLabels, [source === 'saved_batch' ? 'batch_id' : 'run_id']: savedId});
      onStarted(res.task_id);
    } catch (err) {
      onError(err?.body?.detail || err.message);
    } finally {
      setStarting(false);
    }
  };

  const canStart = !starting && !!evaluator && !!evaluation?.metric && (!savedSource || (savedId && selection && !selectionLoading && !selectionError)) && (evaluationOnly || picked.length > 0);

  return (
    <form onSubmit={start} className="evolve-form">
      <section>
        <h4>Run mode</h4>
        <div className="evolve-presets">
          {[['evaluate', 'Evaluation only'], ['evolve_evaluate', 'Evolve + Evaluation']].map(([value, label]) => (
            <label key={value} className={`evolve-preset ${mode === value ? 'selected' : ''}`}>
              <input type="radio" name="run-mode" value={value} checked={mode === value}
                onChange={() => { setMode(value); if (value !== 'evaluate') setSource('canvas'); }} />{label}
            </label>
          ))}
        </div>
        {!evaluation ? <p className="muted small" data-testid="no-evaluation">This workflow has no evaluation code yet. Write it in Evaluation code first.</p>
          : !evaluation.metric ? <p className="muted small" data-testid="no-objective">Choose the metric Evolve optimizes in Evaluation code (preview the code once, then pick it from the report).</p>
          : null}
        {source === 'canvas' && !evaluationOnly && <div className="field"><label htmlFor="canvas-rounds">Candidate rounds</label><input id="canvas-rounds" type="number" min="1" max="10" value={rounds} onChange={e=>setRounds(e.target.value)} /></div>}
        {source === 'canvas' && <div className="field"><label htmlFor="canvas-workers">Records at once</label><input id="canvas-workers" type="number" min="1" max="64" value={workers} onChange={e=>setWorkers(e.target.value)} />
          <small className="muted">Each replay runs like a batch: node by node, in the Input&apos;s own batches, this many records of a batch at a time.</small></div>}
        {source === 'canvas' && !evaluationOnly && <div className="field"><label htmlFor="canvas-held-out">Held out for validation (%)</label><input id="canvas-held-out" type="number" min="10" max="50" value={heldOut} onChange={e=>setHeldOut(e.target.value)} />
          <small className="muted">Whole entities (each Input trajectory with all its records), fixed split. Prompts are proposed and chosen on the rest; the held-out part is scored once at the end and decides nothing.</small></div>}
        <h4>1 · {evaluationOnly ? 'Data to evaluate' : 'Data to learn from'}</h4>
        <div className="evolve-grid">
          <div className="field">
            <label htmlFor="evolve-source">Source</label>
            <select id="evolve-source" value={source} onChange={(e) => setSource(e.target.value)} disabled={!evaluationOnly}>
              <option value="canvas">Canvas Input — run the workflow</option>
              {evaluationOnly && <option value="saved_batch">Saved batch results — no workflow rerun</option>}
              {evaluationOnly && <option value="saved_run">Saved run result — no workflow rerun</option>}
            </select>
            {!evaluationOnly && <small className="muted">Evolve replays the workflow on the canvas Input and holds out part of it to validate; saved results can only be evaluated.</small>}
          </div>
          {savedSource ? <>
            <div className="field"><label htmlFor="evolve-saved">Saved result</label>
              <select id="evolve-saved" value={savedId} onChange={e => setSavedId(e.target.value)}>
                {!savedResults.length && <option value="">No finished results</option>}
                {savedResults.map(r => <option key={source === 'saved_batch' ? r.batch_id : r.run_id} value={source === 'saved_batch' ? r.batch_id : r.run_id}>{source === 'saved_batch' ? r.batch_id : r.run_id} · {r.status} · {r.total != null ? `${r.total} records · ` : ''}{r.created_at ? new Date(r.created_at).toLocaleString() : ''}</option>)}
              </select>
            </div>
            {selectionLoading && <p role="status">Checking saved records…</p>}
            {selectionError && <p role="alert">{selectionError}</p>}
            {selection && <p role="status">{selection.matched_records} matching records{selection.note ? ` · ${selection.note}` : ''}</p>}
          </> : <p className="muted small">Uses the Input configuration and the chosen saved evaluator. Prepares data once; each candidate starts with empty isolated workflow memory. This runs the workflow and may call tools and models.</p>}
        </div>
        <div className="muted small">
          {evaluation?.metric ? <>Scored by the workflow's evaluation code, on <b>{evaluation.metric}</b> ({evaluation.direction === 'minimize' ? 'lower' : 'higher'} is better).</> : 'Scored by the workflow\'s evaluation code.'}
          {source === 'canvas' ? (evaluationOnly ? ' Replays the real workflow on the Input selection, with empty isolated memory, and scores every record.' : ' Replays the real workflow on the Input selection. All candidates replay the same prepared records with empty isolated memory. Before/after scores are on dev; the held-out score is the estimate to trust.') : ' Reads saved predictions and traces. No workflow rerun. Evolution makes one model request to propose prompts from up to 40 saved traces; new prompts are not validated.'}
        </div>
      </section>

      {!evaluationOnly && <section>
        <h4>2 · Prompts it may rewrite</h4>
        {nodes.length === 0 && <div className="muted small">No LLM nodes found on this workflow.</div>}
        <div className="evolve-nodes">
          {nodes.map((name) => (
            <label key={name} className="evolve-node">
              <input type="checkbox" checked={picked.includes(name)} onChange={() => toggleNode(name)} />
              {name}
            </label>
          ))}
        </div>
        <div className="muted small">Untick a node whose prompt you want left exactly as it is.</div>
        <label className="evolve-node"><input type="checkbox" checked={shareLabels} onChange={e => setShareLabels(e.target.checked)} /> Show expected answers to the proposer</label>
        <div className="muted small">Off by default: the model that rewrites the prompts sees inputs, outputs and scores, not the expected answers — a prompt that contains the answers would score well and generalize badly. Tick it only if the labels are part of the instructions you want written.</div>
      </section>}

      <div className="modal-actions">
        <button type="submit" className="primary" disabled={!canStart}>
          {starting ? 'Starting…' : evaluationOnly ? (savedSource ? 'Evaluate saved results' : 'Start evaluation') : savedSource ? 'Evaluate & propose improvements' : 'Start evolution'}
        </button>
      </div>
    </form>
  );
}

