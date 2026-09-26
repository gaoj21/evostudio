import React, { useCallback, useEffect, useState } from 'react';
import { api } from '../../api.js';
import EvaluationCode from './EvaluationCode.jsx';
import { fmt } from './format.js';

// Two things a run can be. An evaluation is code brought to the data: pasted
// or uploaded here, scored on saved results or on a replay of the canvas
// Input. An Evolve continues one of those evaluations: its code is the
// objective, one of its metrics is what gets optimized, and the workflow is
// replayed on the canvas Input with a held-out part to validate on.

const EVALUATION_NAME = 'evaluation';
// The report an evaluation task produced: its metrics are what Evolve can optimize.
const reportOf = (task) => (task?.baseline?.evaluations || {})[EVALUATION_NAME]
  || Object.values(task?.baseline?.evaluations || {})[0] || null;
const when = (t) => (t.created_at ? new Date(t.created_at).toLocaleString() : t.task_id);

export function NewTaskForm({ graphId, onStarted, onError, initialSource = 'saved_batch', initialMode = 'evaluate', initialEvaluation = '' }) {
  const [mode, setMode] = useState(initialMode === 'evolve' ? 'evolve_evaluate' : initialMode);
  const evaluationOnly = mode === 'evaluate';
  const [source, setSource] = useState(initialMode === 'evaluate' ? initialSource : 'canvas');
  const savedSource = evaluationOnly && (source === 'saved_batch' || source === 'saved_run');
  const [code, setCode] = useState({ ready: false });
  const [evaluations, setEvaluations] = useState([]);    // finished evaluations of this workflow
  const [fromEvaluation, setFromEvaluation] = useState(initialEvaluation);
  const [evaluationDetail, setEvaluationDetail] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [metric, setMetric] = useState('');
  const [direction, setDirection] = useState('maximize');
  const [rounds, setRounds] = useState(1);
  const [heldOut, setHeldOut] = useState(30);          // % kept for validation
  const [workers, setWorkers] = useState(4);           // records of a chunk replayed at once
  const [fields, setFields] = useState([]);            // what the Input's records carry
  const [splitField, setSplitField] = useState('');    // '' = each Input trajectory / memory entity
  const [splitOrder, setSplitOrder] = useState('random');
  const [savedResults, setSavedResults] = useState([]);
  const [savedId, setSavedId] = useState('');
  const [selection, setSelection] = useState(null);
  const [selectionError, setSelectionError] = useState('');
  const [selectionLoading, setSelectionLoading] = useState(false);
  const [nodes, setNodes] = useState([]);          // LLM nodes of the graph
  const [chosen, setChosen] = useState(null);      // null = all
  const [shareLabels, setShareLabels] = useState(false);
  const [starting, setStarting] = useState(false);
  const onCode = useCallback((value) => setCode(value), []);

  useEffect(() => {
    if (!graphId) return;
    api.getGraph(graphId)
      .then((g) => {
        setNodes((g.tasks || []).filter((t) => !['source', 'tool'].includes(t.kind)).map((t) => t.name));
        // Any field the Input's records carry can be what dev and val are split on.
        setFields([...new Set((g.tasks || []).filter((t) => t.kind === 'source' && t.enabled !== false)
          .flatMap((t) => (t.outputs || []).map((o) => (typeof o === 'string' ? o : o?.name)).filter(Boolean)))]);
      })
      .catch(() => { setNodes([]); setFields([]); });
    api.listEvolveTasks(graphId)
      .then((list) => {
        // Only an evaluation that kept its code can be continued: older ones
        // scored with a built-in metric, which no longer exists.
        const done = (list || []).filter((t) => t.params?.mode === 'evaluate' && t.status === 'done'
          && (t.params?.evaluator_entry || t.params?.evaluator));
        setEvaluations(done);
        setFromEvaluation((current) => current || done[0]?.task_id || '');
      })
      .catch(() => setEvaluations([]));
  }, [graphId]);

  // The evaluation Evolve continues: the metrics its report has.
  useEffect(() => {
    let active = true;
    setEvaluationDetail(null);
    if (evaluationOnly || !fromEvaluation) return undefined;
    setDetailLoading(true);
    api.getEvolveTask(fromEvaluation).finally(() => { if (active) setDetailLoading(false); }).then((task) => {
      if (!active) return;
      setEvaluationDetail(task);
      const names = Object.keys(reportOf(task)?.metrics || {});
      const kept = task.params?.evaluator_entry;
      setMetric((current) => (names.includes(current) ? current : kept?.metric && names.includes(kept.metric) ? kept.metric : names[0] || ''));
      if (kept?.direction) setDirection(kept.direction);
    }).catch((err) => { if (active) onError(err?.body?.detail || err.message); });
    return () => { active = false; };
  }, [fromEvaluation, evaluationOnly]);

  useEffect(() => {
    let active = true;
    setSavedResults([]); setSavedId('');
    if (savedSource && graphId) {
      const request = source === 'saved_batch' ? api.listBatches(graphId) : api.listRuns(graphId);
      request.then(rows => { if (active) { const done = rows.filter(r => !['running', 'pending', 'queued'].includes(r.status)); setSavedResults(done); setSavedId((source === 'saved_batch' ? done[0]?.batch_id : done[0]?.run_id) || ''); } }).catch(() => { if (active) onError('Unable to load saved results.'); });
    }
    return () => { active = false; };
  }, [graphId, source, savedSource]);

  useEffect(() => {
    let active = true;
    setSelection(null); setSelectionError('');
    if (!savedSource || !savedId) { setSelectionLoading(false); return () => { active = false; }; }
    setSelectionLoading(true);
    api.previewEvolveResults(graphId, { source, [source === 'saved_batch' ? 'batch_id' : 'run_id']: savedId }).then(value => {
      if (active) setSelection(value);
    }).catch(err => { if (active) setSelectionError(err?.body?.detail || err.message); })
      .finally(() => { if (active) setSelectionLoading(false); });
    return () => { active = false; };
  }, [graphId, source, savedId, savedSource]);

  const picked = chosen === null ? nodes : chosen;
  const toggleNode = (name) => setChosen((current) => {
    const base = current === null ? nodes : current;
    return base.includes(name) ? base.filter((x) => x !== name) : [...base, name];
  });
  const metrics = Object.keys(reportOf(evaluationDetail)?.metrics || {});

  const start = async (e) => {
    e.preventDefault();
    setStarting(true);
    try {
      const evaluation = { code: code.code, config: code.config, timeout: code.timeout, labels: code.labels };
      const body = evaluationOnly
        ? (source === 'canvas'
          ? { source: 'canvas', mode: 'evaluate', ...evaluation, workers: Number(workers) }
          : { source, mode: 'evaluate', ...evaluation, [source === 'saved_batch' ? 'batch_id' : 'run_id']: savedId })
        : { source: 'canvas', mode: 'evolve', from_evaluation: fromEvaluation, metric, direction, nodes: picked,
          rounds: Number(rounds), share_labels: shareLabels, workers: Number(workers),
          val_fraction: Number(heldOut) / 100, split_field: splitField || null, split_order: splitField ? splitOrder : 'random' };
      const res = await api.startEvolveResults(graphId, body);
      onStarted(res.task_id);
    } catch (err) {
      onError(err?.body?.detail || err.message);
    } finally {
      setStarting(false);
    }
  };

  const canStart = !starting && (evaluationOnly
    ? code.ready && (!savedSource || (savedId && selection && !selectionLoading && !selectionError))
    : !!fromEvaluation && !!metric && picked.length > 0);

  return (
    <form onSubmit={start} className="evolve-form">
      <section>
        <h4>Run mode</h4>
        <div className="evolve-presets">
          {[['evaluate', 'Evaluation'], ['evolve_evaluate', 'Evolve']].map(([value, label]) => (
            <label key={value} className={`evolve-preset ${mode === value ? 'selected' : ''}`}>
              <input type="radio" name="run-mode" value={value} checked={mode === value}
                onChange={() => { setMode(value); if (value !== 'evaluate') setSource('canvas'); }} />{label}
            </label>
          ))}
        </div>
      </section>

      {evaluationOnly ? <>
        <section>
          <h4>1 · Evaluation code</h4>
          <EvaluationCode graphId={graphId} onChange={onCode} disabled={starting} />
        </section>
        <section>
          <h4>2 · Data to evaluate</h4>
          <div className="evolve-grid">
            <div className="field">
              <label htmlFor="evolve-source">Source</label>
              <select id="evolve-source" value={source} onChange={(e) => setSource(e.target.value)}>
                <option value="saved_batch">Saved batch results — no workflow rerun</option>
                <option value="saved_run">Saved run result — no workflow rerun</option>
                <option value="canvas">Canvas Input — run the workflow</option>
              </select>
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
              {selection && <p role="status">{selection.matched_records} records to evaluate</p>}
            </> : <div className="field"><label htmlFor="canvas-workers">Records at once</label><input id="canvas-workers" type="number" min="1" max="64" value={workers} onChange={e=>setWorkers(e.target.value)} />
              <small className="muted">Replays the workflow on the canvas Input like a batch — node by node, in the Input&apos;s own batches — with empty isolated memory, then scores every record.</small></div>}
          </div>
          <p className="muted small">The report is kept with this evaluation{savedSource ? ' and with that saved result' : ''}. Evolve can continue it: its code is what Evolve optimizes.</p>
        </section>
      </> : <>
        <section>
          <h4>1 · The evaluation it continues</h4>
          {evaluations.length === 0
            ? <p className="muted small" data-testid="no-evaluation">No finished evaluation yet. Run an Evaluation first: Evolve optimizes what its code scores.</p>
            : <div className="evolve-grid">
              <div className="field"><label htmlFor="evolve-from">Evaluation to continue</label>
                <select id="evolve-from" value={fromEvaluation} onChange={(e) => setFromEvaluation(e.target.value)}>
                  {evaluations.map((t) => <option key={t.task_id} value={t.task_id}>{when(t)}{t.baseline_score != null ? ` · score ${fmt(t.baseline_score)}` : ''}</option>)}
                </select></div>
              <div className="field"><label htmlFor="evolve-metric">Optimize</label>
                <select id="evolve-metric" value={metric} onChange={(e) => setMetric(e.target.value)} disabled={!metrics.length}>
                  {!metrics.length && <option value="">{detailLoading ? 'Loading its metrics…' : 'Its report has no metrics'}</option>}
                  {metrics.map((m) => <option key={m} value={m}>{m}</option>)}
                </select></div>
              <div className="field"><label htmlFor="evolve-direction">Better is</label>
                <select id="evolve-direction" value={direction} onChange={(e) => setDirection(e.target.value)}>
                  <option value="maximize">Higher</option><option value="minimize">Lower</option>
                </select></div>
            </div>}
          <p className="muted small">Evolve uses that evaluation&apos;s code, parameters and labels, and replays the workflow on the canvas Input: all candidates the same prepared records, each with empty isolated memory.</p>
        </section>
        <section>
          <h4>2 · How it learns and validates</h4>
          <div className="field"><label htmlFor="canvas-rounds">Candidate rounds</label><input id="canvas-rounds" type="number" min="1" max="10" value={rounds} onChange={e=>setRounds(e.target.value)} /></div>
          <div className="field"><label htmlFor="canvas-workers">Records at once</label><input id="canvas-workers" type="number" min="1" max="64" value={workers} onChange={e=>setWorkers(e.target.value)} />
            <small className="muted">Each replay runs like a batch: node by node, in the Input&apos;s own batches, this many records of a batch at a time.</small></div>
          <div className="field"><label htmlFor="canvas-held-out">Held out for validation (%)</label><input id="canvas-held-out" type="number" min="10" max="50" value={heldOut} onChange={e=>setHeldOut(e.target.value)} />
            <small className="muted">Prompts are proposed and chosen on the rest; the held-out part is scored once at the end and decides nothing.</small></div>
          <div className="field"><label htmlFor="canvas-split-field">Split dev / val by</label>
            <select id="canvas-split-field" value={splitField} onChange={e=>{ setSplitField(e.target.value); if (!e.target.value) setSplitOrder('random'); }}>
              <option value="">Each Input trajectory / memory entity</option>
              {fields.map((f) => <option key={f} value={f}>{f}</option>)}
            </select>
            {splitField && <><label htmlFor="canvas-split-order">Held-out values</label>
              <select id="canvas-split-order" value={splitOrder} onChange={e=>setSplitOrder(e.target.value)}>
                <option value="random">At random (a fixed split of the {splitField} values)</option>
                <option value="latest">The latest {splitField} values (dev is everything before)</option>
              </select></>}
            <small className="muted">Every record with the same value goes to the same side. Whatever the split, every candidate replays every record, so memory builds up as it would in a real run.</small></div>
        </section>
        <section>
          <h4>3 · Prompts it may rewrite</h4>
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
          <div className="muted small">Off by default: the model that rewrites the prompts sees inputs, outputs and scores, not the expected answers — a prompt that contains the answers would score well and generalize badly.</div>
        </section>
      </>}

      <div className="modal-actions">
        <button type="submit" className="primary" disabled={!canStart}>
          {starting ? 'Starting…' : evaluationOnly ? (savedSource ? 'Evaluate saved results' : 'Start evaluation') : 'Start evolution'}
        </button>
      </div>
    </form>
  );
}
