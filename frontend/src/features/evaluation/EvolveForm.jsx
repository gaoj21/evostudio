import React, { useEffect, useState } from 'react';
import { api } from '../../api.js';

export function NewTaskForm({ graphId, onStarted, onError, initialSource = 'saved_batch', initialMode = 'evaluate' }) {
  const [evaluators, setEvaluators] = useState([]);
  const [evaluator, setEvaluator] = useState('');
  const [rounds, setRounds] = useState(1);
  const [mode, setMode] = useState(initialMode);
  const evaluationOnly = mode === 'evaluate';
  const [source, setSource] = useState(initialSource);
  const savedSource = source === 'saved_batch' || source === 'saved_run';
  const [savedResults, setSavedResults] = useState([]);
  const [savedId, setSavedId] = useState('');
  const [savedDataset, setSavedDataset] = useState('');
  const [savedSplit, setSavedSplit] = useState('');
  const [selection, setSelection] = useState(null);
  const [selectionError, setSelectionError] = useState('');
  const [selectionLoading, setSelectionLoading] = useState(false);
  const [labelKey, setLabelKey] = useState('');
  const [dataset, setDataset] = useState('contemporary');
  const [datasetLoading, setDatasetLoading] = useState(true);
  const [file, setFile] = useState(null);
  const [metrics, setMetrics] = useState([]);
  const [metric, setMetric] = useState('');
  const [sourceInfo, setSourceInfo] = useState(null);
  const [split, setSplit] = useState('dev');
  const [n, setN] = useState(6);
  const [seed, setSeed] = useState(42);
  const [presets, setPresets] = useState([]);
  const [preset, setPreset] = useState('quick');
  const [nodes, setNodes] = useState([]);          // LLM nodes of the graph
  const [chosen, setChosen] = useState(null);      // null = all
  const [advanced, setAdvanced] = useState(false);
  const [adv, setAdv] = useState({ n_train: '', n_dev: '', num_candidates: '', max_steps: '', seed: '9' });
  const [starting, setStarting] = useState(false);

  useEffect(() => {
    api.evolveMetrics().then((r) => setMetrics(r.metrics || [])).catch(() => setMetrics([]));
    api.evolvePresets().then((r) => setPresets(r.presets || [])).catch(() => setPresets([]));
    let active = true;
    setDatasetLoading(true);
    api.creditRiskSource(dataset).then(info => { if (active) { setSourceInfo(info); if (info.dataset && info.dataset !== dataset) setDataset(info.dataset); } }).catch(() => { if (active) setSourceInfo(null); }).finally(() => { if (active) setDatasetLoading(false); });
    if (graphId) {
      api.getGraph(graphId)
        .then((g) => { setNodes((g.tasks || []).filter((t) => !['source', 'tool', 'evaluator'].includes(t.kind)).map((t) => t.name)); setEvaluators((g.tasks || []).filter(t=>t.kind==='evaluator' && t.enabled!==false)); })
        .catch(() => setNodes([]));
    }
    return () => { active = false; };
  }, [graphId, dataset]);

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
    api.previewEvolveResults(graphId, {source, ...(evaluator ? {evaluator} : {}), dataset: savedDataset || undefined, split: savedSplit, label_key: labelKey, metric: metric || undefined,
      [source === 'saved_batch' ? 'batch_id' : 'run_id']: savedId}).then(value => {
        if (active) setSelection(value);
      }).catch(err => { if (active) setSelectionError(err?.body?.detail || err.message); })
      .finally(() => { if (active) setSelectionLoading(false); });
    return () => { active = false; };
  }, [graphId, source, savedId, savedDataset, savedSplit, labelKey, metric, evaluator]);

  const effectiveMetric = evaluator && (savedSource || source === 'canvas') ? `canvas:${evaluator}` : metric || (source === 'credit_risk' ? 'credit_risk' : savedSource ? selection?.suggested_metric || 'exact_match' : 'exact_match');
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
      const extra = {};
      if (advanced && !evaluationOnly) {
        Object.entries(adv).forEach(([k, v]) => { if (v !== '') extra[k] = Number(v); });
      }
      const params = { mode, metric: effectiveMetric, preset, nodes: picked.join(','), ...(source === 'credit_risk' && dataset !== 'contemporary' ? { dataset } : {}), ...extra };
      const res = source === 'canvas'
        ? await api.startEvolveResults(graphId,{source:'canvas',mode,evaluator,nodes:evaluationOnly?[]:picked,rounds:Number(rounds)})
        : savedSource
        ? await api.startEvolveResults(graphId, {source, mode, ...(evaluator ? {evaluator} : {}), metric: effectiveMetric, nodes: evaluationOnly ? [] : picked, dataset: savedDataset || undefined, split: savedSplit, label_key: labelKey, [source === 'saved_batch' ? 'batch_id' : 'run_id']: savedId})
        : source === 'upload'
        ? await api.startEvolveUpload(graphId, file, params)
        : await api.startEvolveSource(graphId, {
          ...params, split: split || undefined, n: Number(n), seed: Number(seed), nodes: picked,
        });
      onStarted(res.task_id);
    } catch (err) {
      onError(err?.body?.detail || err.message);
    } finally {
      setStarting(false);
    }
  };

  const splits = Object.keys(sourceInfo?.splits || {});
  const chosenPreset = presets.find((p) => p.name === preset);
  const eligible = sourceInfo?.evolve_splits ? (split ? sourceInfo.evolve_splits[split] || 0 : Object.values(sourceInfo.evolve_splits).reduce((a,b) => a+b, 0)) : null;
  const canStart = !starting && (source !== 'canvas' || !!evaluator) && (!savedSource || (savedId && selection && !selectionLoading && !selectionError)) && (source !== 'upload' || file) && (evaluationOnly || picked.length > 0) && (source !== 'credit_risk' || (!datasetLoading && sourceInfo && (eligible === null || eligible >= (evaluationOnly ? 1 : 2))));

  return (
    <form onSubmit={start} className="evolve-form">
      <section>
        <h4>Run mode</h4>
        <div className="evolve-presets">
          {[['evaluate', 'Evaluation only'], ['evolve_evaluate', 'Evolve + Evaluation']].map(([value, label]) => (
            <label key={value} className={`evolve-preset ${mode === value ? 'selected' : ''}`}>
              <input type="radio" name="run-mode" value={value} checked={mode === value} onChange={() => setMode(value)} />{label}
            </label>
          ))}
        </div>
        {(savedSource || source === 'canvas') && <div className="field"><label htmlFor="canvas-evaluator">Canvas evaluator</label><select id="canvas-evaluator" value={evaluator} onChange={e=>setEvaluator(e.target.value)}><option value="">{source === 'canvas' ? 'Choose an evaluator' : 'Use existing metric'}</option>{evaluators.map(t=><option key={t.name} value={t.name}>{t.name} · {t.evaluator?.type}</option>)}</select></div>}
        {source === 'canvas' && !evaluationOnly && <div className="field"><label htmlFor="canvas-rounds">Candidate rounds</label><input id="canvas-rounds" type="number" min="1" max="10" value={rounds} onChange={e=>setRounds(e.target.value)} /></div>}
        <h4>1 · {evaluationOnly ? 'Data to evaluate' : 'Data to learn from'}</h4>
        <div className="evolve-grid">
          <div className="field">
            <label htmlFor="evolve-source">Source</label>
            <select id="evolve-source" value={source} onChange={(e) => setSource(e.target.value)}>
              <option value="canvas">Canvas DataLoader + Evaluator — run workflow</option>
              <option value="saved_batch">Saved batch results — no workflow rerun</option>
              <option value="saved_run">Saved run result — no workflow rerun</option>
              <option value="credit_risk">Credit-risk dataset — rerun workflow</option>
              <option value="upload">Upload JSON / JSONL — rerun workflow</option>
            </select>
          </div>
          {savedSource ? <>
            <div className="field"><label htmlFor="evolve-saved">Saved result</label>
              <select id="evolve-saved" value={savedId} onChange={e => setSavedId(e.target.value)}>
                {!savedResults.length && <option value="">No finished results</option>}
                {savedResults.map(r => <option key={source === 'saved_batch' ? r.batch_id : r.run_id} value={source === 'saved_batch' ? r.batch_id : r.run_id}>{source === 'saved_batch' ? r.batch_id : r.run_id} · {r.status} · {r.total != null ? `${r.total} records · ` : ''}{r.created_at ? new Date(r.created_at).toLocaleString() : ''}</option>)}
              </select>
            </div>
            <div className="field"><label htmlFor="evolve-saved-dataset">Dataset version</label>
              <select id="evolve-saved-dataset" value={savedDataset} onChange={e => setSavedDataset(e.target.value)}>
                <option value="">Original dataset from run</option>
                {(sourceInfo?.datasets || []).map(d => <option key={d.id} value={d.id}>{d.label}</option>)}
              </select>
            </div>
            <div className="field"><label htmlFor="evolve-saved-split">Split</label>
              <select id="evolve-saved-split" value={savedSplit} onChange={e => setSavedSplit(e.target.value)}>
                <option value="">All saved splits</option><option value="dev">dev</option><option value="test">test</option>
              </select>
            </div>
            {selectionLoading && <p role="status">Checking saved records…</p>}
            {selectionError && <p role="alert">{selectionError}</p>}
            {selection && <p role="status">{selection.matched_records} matching records · {selection.matched_cases} trajectories
              {selection.dataset && ` · ${selection.dataset} / ${selection.split || 'all'}`}
              {selection.missing_cases > 0 && ` · ${selection.missing_cases} dataset trajectories have no saved result; they will not be rerun.`}
            </p>}
            {selection?.scoring && <p role="status">{selection.scoring.scored} records can be scored · {selection.scoring.unscored} unscored. {selection.scoring.scored === 0 && 'Choose the expected-answer field and metric before evaluating.'}</p>}
            <div className="field"><label htmlFor="evolve-label-key">Expected-answer field (optional)</label><input id="evolve-label-key" value={labelKey} onChange={e => setLabelKey(e.target.value)} placeholder="Uses saved labels when available" /></div>
          </> : source === 'canvas' ? <p className="muted small">Uses the Input configuration and attached evaluator. Prepares data once; each candidate starts with empty isolated workflow memory. This runs the workflow and may call tools and models.</p> : source === 'upload' ? (
            <div className="field">
              <label htmlFor="evolve-file">File</label>
              <input id="evolve-file" type="file" accept=".json,.jsonl" onChange={(e) => setFile(e.target.files?.[0] || null)} />
            </div>
          ) : (
            <>
              <div className="field">
                <label htmlFor="evolve-dataset">Dataset version</label>
                <select id="evolve-dataset" value={dataset} disabled={starting} onChange={e => { setDataset(e.target.value); setSplit('dev'); }}>
                  {(sourceInfo?.datasets || [{id:'contemporary',label:'Contemporary (legacy)'}]).map(d => <option key={d.id} value={d.id}>{d.label}</option>)}
                </select>
              </div>
              <div className="field">
                <label htmlFor="evolve-split">Split</label>
                <select id="evolve-split" value={split} onChange={(e) => setSplit(e.target.value)}>
                  <option value="">(all)</option>
                  {splits.map((s) => <option key={s} value={s}>{s} ({sourceInfo.splits[s]} {dataset === 'contemporary' ? 'samples' : 'trajectories'})</option>)}
                </select>
              </div>
              <div className="field">
                <label htmlFor="evolve-n">Samples</label>
                <input id="evolve-n" type="number" min={evaluationOnly ? "1" : "2"} value={n} onChange={(e) => setN(e.target.value)} />
              </div>
              <div className="field">
                <label htmlFor="evolve-seed">Seed</label>
                <input id="evolve-seed" type="number" value={seed} onChange={(e) => setSeed(e.target.value)} />
              </div>
            </>
          )}
        </div>
        <div className="muted small">
          Scored with <b>{effectiveMetric}</b>
          {source === 'credit_risk' ? ' — the dataset\'s own truth; ' : '; '}
          <button type="button" className="link small" onClick={() => setAdvanced(true)}>change</button>.
          {source === 'canvas' ? ' Replays the real workflow on the Input selection. All candidates use the same prepared records and empty isolated memory. Scores are development scores; use a separate Test run for final validation.' : savedSource ? ' Reads saved predictions and traces. No workflow rerun. Missing labels remain unscored. Evolution makes one model request to propose prompts from up to 40 saved traces; new prompts are not validated.' : evaluationOnly ? ' All selected records are scored with the current prompts. No optimization is performed.' : ' The selected split is divided internally: about 70% for training and 30% for candidate validation. Before/after scores use that validation subset, not an independent test set.'}
          {!savedSource && source !== 'canvas' && ' This evaluates window snapshots; it does not replay the canvas trajectory or its memory.'}
        </div>
      </section>

      {source === 'credit_risk' && !datasetLoading && !sourceInfo && <p role="alert">Unable to load dataset information. Reopen this panel to retry.</p>}
      {source === 'credit_risk' && sourceInfo?.evolve_note && <div className="muted small" role="status">
        <p>{eligible} eligible cases in this selection. {sourceInfo.evolve_note}</p>
      </div>}
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
      </section>}

      <section>
        {!evaluationOnly && !savedSource && source !== 'canvas' && <h4>3 · How hard to try</h4>}
        {!evaluationOnly && !savedSource && source !== 'canvas' && <div className="evolve-presets">
          {(presets.length ? presets : [{ name: 'quick', label: 'Quick', blurb: '' }]).map((p) => (
            <label key={p.name} className={`evolve-preset ${preset === p.name ? 'selected' : ''}`}>
              <input type="radio" name="preset" value={p.name} checked={preset === p.name} onChange={() => setPreset(p.name)} />
              <span className="evolve-preset-name">{p.label}</span>
              <span className="muted small">{p.blurb}</span>
            </label>
          ))}
        </div>
        }
        <button type="button" className="link small" onClick={() => setAdvanced((v) => !v)}>
          {advanced ? 'Hide advanced' : 'Advanced…'}
        </button>
        {advanced && (
          <div className="evolve-grid evolve-advanced">
            <div className="field">
              <label htmlFor="evolve-metric">Metric</label>
              <select id="evolve-metric" disabled={!!evaluator} value={effectiveMetric} onChange={(e) => setMetric(e.target.value)}>
                {evaluator && <option value={effectiveMetric}>{effectiveMetric}</option>}
                {metrics.map((m) => <option key={m.name} value={m.name}>{m.name}</option>)}
                {metrics.length === 0 && <option value={effectiveMetric}>{effectiveMetric}</option>}
              </select>
            </div>
            {(evaluationOnly || savedSource || source === 'canvas' ? [] : [['n_train', 'Train records'], ['n_dev', 'Judge records'], ['num_candidates', 'Candidates'], ['max_steps', 'Rounds'], ['seed', 'Optimizer seed']]).map(([key, label]) => (
              <div className="field" key={key}>
                <label htmlFor={`evolve-${key}`}>{label}</label>
                <input
                  id={`evolve-${key}`}
                  type="number"
                  min="1"
                  placeholder={key === 'num_candidates' ? String(chosenPreset?.num_candidates ?? '') : key === 'max_steps' ? String(chosenPreset?.max_steps ?? '') : 'auto'}
                  value={adv[key]}
                  onChange={(e) => setAdv((a) => ({ ...a, [key]: e.target.value }))}
                />
              </div>
            ))}
          </div>
        )}
      </section>

      <div className="modal-actions">
        <button type="submit" className="primary" disabled={!canStart}>
          {starting ? 'Starting…' : evaluationOnly ? (savedSource ? 'Evaluate saved results' : 'Start evaluation') : savedSource ? 'Evaluate & propose improvements' : 'Start optimization'}
        </button>
      </div>
    </form>
  );
}

