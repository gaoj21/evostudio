import React, { useEffect, useRef, useState } from 'react';
import { api } from '../api.js';

const INPUTS_KEY = 'evoagentx-studio:last-inputs';

// Past this many runs the size of the batch is worth saying out loud:
// every run is a full pass of the workflow, so it is nodes times this.
const BIG_BATCH = 20;

// The framework validates workflow inputs against their declared JSON-schema
// type, so a number field really must arrive as a number: sending "5" for an
// `int` input is rejected outright. Every value is parsed to its declared type
// before it leaves this form.
export function parseValue(type, raw) {
  const t = String(type || 'str').toLowerCase();
  if (t === 'bool' || t === 'boolean') return { value: !!raw };
  if (['int', 'integer'].includes(t)) {
    if (raw === '' || raw === null || raw === undefined) return { value: undefined };
    if (!/^-?\d+$/.test(String(raw).trim())) return { error: 'must be a whole number' };
    return { value: parseInt(raw, 10) };
  }
  if (['float', 'number'].includes(t)) {
    if (raw === '' || raw === null || raw === undefined) return { value: undefined };
    const n = Number(raw);
    if (Number.isNaN(n)) return { error: 'must be a number' };
    return { value: n };
  }
  if (['list', 'array', 'dict', 'object', 'json'].includes(t)) {
    if (raw === '' || raw === null || raw === undefined) return { value: undefined };
    try {
      const parsed = JSON.parse(raw);
      const wantArray = ['list', 'array'].includes(t);
      if (wantArray && !Array.isArray(parsed)) return { error: 'must be a JSON array' };
      if (!wantArray && t !== 'json' && (Array.isArray(parsed) || typeof parsed !== 'object')) {
        return { error: 'must be a JSON object' };
      }
      return { value: parsed };
    } catch (e) {
      return { error: `invalid JSON: ${e.message}` };
    }
  }
  return { value: raw ?? '' };
}

export function defaultFor(type) {
  const t = String(type || 'str').toLowerCase();
  if (t === 'bool' || t === 'boolean') return false;
  if (['list', 'array'].includes(t)) return '[]';
  if (['dict', 'object', 'json'].includes(t)) return '{}';
  return '';
}

export function toField(type, value) {
  // The form edits text (or a checkbox); a prefilled value arrives in its real
  // type, so structured values come back as the JSON the field expects.
  const t = String(type || 'str').toLowerCase();
  if (t === 'bool' || t === 'boolean') return !!value;
  if (value === null || value === undefined) return defaultFor(type);
  return typeof value === 'string' ? value : JSON.stringify(value, null, 2);
}

function SingleRunForm({ graphId, workflowInputs, hasCanvasSource, onCancel, onSubmit, submitting }) {
  const [values, setValues] = useState({});
  const [touched, setTouched] = useState(false);
  // Starting part-way through: the nodes before the start are left out and
  // their outputs become inputs, usually taken from an earlier run.
  const [startAt, setStartAt] = useState('');
  // Runs sharing a session are reminded of what the others did, in order.
  // Blank means this run stands alone, which is what most runs should be.
  const [session, setSession] = useState('');
  const [plan, setPlan] = useState(null);
  const [planning, setPlanning] = useState(false);

  const fields = plan ? plan.workflow_inputs : workflowInputs;

  useEffect(() => {
    if (!graphId) return;
    setPlanning(true);
    api
      .graphInputs(graphId, startAt ? [startAt] : [])
      .then((p) => setPlan(p))
      .catch(() => setPlan(null))
      .finally(() => setPlanning(false));
  }, [graphId, startAt]);

  useEffect(() => {
    // Re-running while iterating is the common case, so the last values used
    // for this workflow come back pre-filled; an earlier run's outputs win for
    // the inputs that only exist because the run starts part-way through.
    let saved = {};
    try {
      saved = JSON.parse(localStorage.getItem(`${INPUTS_KEY}:${graphId}`)) || {};
    } catch {
      saved = {};
    }
    const prefill = plan?.prefill || {};
    const init = {};
    (fields || []).forEach((w) => {
      const source = prefill[w.name] !== undefined ? prefill[w.name] : saved[w.name];
      init[w.name] = source !== undefined ? toField(w.type, source) : defaultFor(w.type);
    });
    setValues(init);
    setTouched(false);
  }, [fields, plan, graphId]);

  const parsed = (fields || []).map((w) => ({ input: w, ...parseValue(w.type, values[w.name]) }));
  const errors = parsed.filter((p) => p.error);
  const missing = parsed.filter(
    (p) => p.input.required && !p.error && (p.value === undefined || p.value === '')
  );

  const submit = (e) => {
    e.preventDefault();
    setTouched(true);
    if (errors.length || missing.length) return;
    const payload = {};
    parsed.forEach((p) => {
      if (p.value !== undefined) payload[p.input.name] = p.value;
    });
    try {
      localStorage.setItem(`${INPUTS_KEY}:${graphId}`, JSON.stringify(values));
    } catch {
      /* storage unavailable; the run still proceeds */
    }
    onSubmit(payload, startAt ? [startAt] : undefined, session.trim() || undefined);
  };

  const set = (name, value) => setValues((v) => ({ ...v, [name]: value }));

  return (
    <form onClick={(e) => e.stopPropagation()} onSubmit={submit}>
      {(plan?.nodes || []).length > 1 && (
        <div className="field">
          <label>Start from</label>
          <select value={startAt} onChange={(e) => setStartAt(e.target.value)}>
            <option value="">The beginning (whole workflow)</option>
            {(plan?.nodes || []).map((name) => (
              <option key={name} value={name}>
                {name} and everything after it
              </option>
            ))}
          </select>
          {startAt && (
            <div className="muted small">
              Nodes before <span className="tool-option-name">{startAt}</span> are skipped;
              what they would have produced is asked for below.
            </div>
          )}
        </div>
      )}
      <div className="field">
        <label>
          Session <span className="muted">(optional)</span>
        </label>
        <input
          value={session}
          onChange={(e) => setSession(e.target.value)}
          placeholder="e.g. review-2026-09"
        />
        <div className="muted small">
          Short-term memory. Runs given the same session are told what the
          others did, in order. Leave it blank and this run stands alone.
        </div>
      </div>
      {plan?.prefill_from_run && Object.keys(plan.prefill || {}).length > 0 && (
        <div className="muted small prefill-note">
          Filled in from run <span className="tool-option-name">{plan.prefill_from_run}</span>:{' '}
          {Object.keys(plan.prefill).join(', ')}
        </div>
      )}
      {hasCanvasSource && !startAt && (
        <p className="muted small">Inputs come from the canvas source node; sampled at run time.</p>
      )}
      {planning && <p className="muted small">Working out what this run needs…</p>}
      {!planning && (fields || []).length === 0 && !hasCanvasSource && (
        <p className="muted">This workflow has no external inputs.</p>
      )}
      {parsed.map(({ input: w, error }) => {
        const type = String(w.type || 'str').toLowerCase();
        const isBool = type === 'bool' || type === 'boolean';
        const isNumber = ['int', 'integer', 'float', 'number'].includes(type);
        const isJson = ['list', 'array', 'dict', 'object', 'json'].includes(type);
        return (
          <div className="field" key={w.name}>
            <label>
              {w.name}{' '}
              <span className="muted">
                ({w.type || 'str'}
                {w.required ? ', required' : ''})
              </span>
              {w.consumed_by && <span className="muted small"> → {w.consumed_by}</span>}
            </label>
            {w.description && <div className="muted small">{w.description}</div>}
            {isBool ? (
              <label className="param-req">
                <input type="checkbox" checked={!!values[w.name]} onChange={(e) => set(w.name, e.target.checked)} />
                {values[w.name] ? 'true' : 'false'}
              </label>
            ) : isNumber ? (
              <input
                type="number"
                step={['int', 'integer'].includes(type) ? '1' : 'any'}
                value={values[w.name] ?? ''}
                onChange={(e) => set(w.name, e.target.value)}
              />
            ) : (
              <textarea
                className={isJson ? 'code-input' : undefined}
                rows={isJson ? 3 : 2}
                value={values[w.name] ?? ''}
                onChange={(e) => set(w.name, e.target.value)}
              />
            )}
            {touched && error && <div className="chat-error">{w.name} {error}</div>}
          </div>
        );
      })}
      {touched && missing.length > 0 && (
        <div className="chat-error">Required: {missing.map((p) => p.input.name).join(', ')}</div>
      )}
      <div className="modal-actions">
        <button type="button" onClick={onCancel} disabled={submitting}>
          Cancel
        </button>
        <button type="submit" className="primary" disabled={submitting}>
          {submitting ? 'Starting…' : 'Run'}
        </button>
      </div>
    </form>
  );
}

// What the preview says in one line. Stepping is the case worth spelling out:
// `n` samples become one record per date, so the number of runs is not the
// number you typed.
export function previewSummary(p) {
  if (!p) return null;
  const runs = `${p.total} run${p.total === 1 ? '' : 's'}`;
  if (!p.steps || p.steps <= 1 || !p.samples || p.samples >= p.total) return runs;
  const span = p.dates && p.dates[0] !== p.dates[1]
    ? ` covering ${p.dates[0]} to ${p.dates[1]}` : '';
  // Windows differ per sample, so the number of dates can too.
  const each = p.steps_min && p.steps_min !== p.steps
    ? `${p.steps_min}–${p.steps} dates each` : `${p.steps} dates each`;
  return `${runs} — ${p.samples} sample${p.samples === 1 ? '' : 's'} `
    + `stepped over ${each}${span}`;
}

// The count, above the Run button. A batch is the expensive thing in this app:
// it is worth a line of the dialog to say how big the one you are about to
// start actually is.
function BatchPreview({ preview, loading, error, idle }) {
  if (idle) return null;
  if (error) {
    return <div className="muted small batch-error">Cannot read this source: {String(error)}</div>;
  }
  if (loading && !preview) return <div className="muted small batch-preview">Counting records…</div>;
  if (!preview) return null;
  return (
    <div className={`batch-preview${preview.total >= BIG_BATCH ? ' batch-preview-big' : ''}`}>
      <strong>{previewSummary(preview)}</strong>
      {preview.total >= BIG_BATCH && (
        <div className="muted small">
          Each run is a full pass of the workflow. This one is large.
        </div>
      )}
      {preview.fields?.length > 0 && (
        <div className="muted small">Each run gets: {preview.fields.join(', ')}</div>
      )}
    </div>
  );
}

function BatchRunForm({ graphId, hasCanvasSource, onCancel, beforeRun, onBatchStart }) {
  // A graph with a source node on it has already said where its data comes
  // from; making you pick a file again is the wrong default.
  const [source, setSource] = useState(hasCanvasSource ? 'canvas' : 'upload');
  const [file, setFile] = useState(null);
  const [sourceInfo, setSourceInfo] = useState(null);
  const [split, setSplit] = useState('');
  const [n, setN] = useState(3);
  const [fullSplit, setFullSplit] = useState(false);
  const [seed, setSeed] = useState(42);
  // Walking the window instead of handing it over whole. It multiplies the
  // batch, which is why the count above the button matters.
  const [step, setStep] = useState('none');
  const [error, setError] = useState(null);
  const [starting, setStarting] = useState(false);
  const [workers, setWorkers] = useState(2);
  // An evaluation is this same batch with a metric attached; leaving the metric
  // empty runs it as an ordinary batch.
  const [metric, setMetric] = useState('');
  const [labelKey, setLabelKey] = useState('');
  const [metrics, setMetrics] = useState([]);
  const [preview, setPreview] = useState(null);
  const [previewing, setPreviewing] = useState(false);
  const [previewError, setPreviewError] = useState(null);

  useEffect(() => {
    api.creditRiskSource().then(setSourceInfo).catch(() => setSourceInfo(null));
    api.listMetrics().then((r) => setMetrics(r.metrics || [])).catch(() => setMetrics([]));
  }, []);

  // Held in a ref so re-rendering the parent does not re-run the preview.
  const beforeRunRef = useRef(beforeRun);
  beforeRunRef.current = beforeRun;

  // Ask the server what this configuration would actually run. It resolves the
  // records the same way the batch does, so the count is the real one.
  useEffect(() => {
    if (source === 'upload' && !file) {
      setPreview(null);
      setPreviewError(null);
      return undefined;
    }
    let cancelled = false;
    setPreviewing(true);
    const timer = setTimeout(async () => {
      try {
        // The canvas source node's config lives in the graph, so the saved
        // version has to match the screen for the count to be true.
        const saved = source === 'canvas' ? await beforeRunRef.current?.() : null;
        const activeGraphId = saved?.id || graphId;
        const res = source === 'upload'
          ? await api.previewBatchUpload(activeGraphId, file)
          : source === 'canvas'
            ? await api.previewBatchCanvas(activeGraphId)
            : await api.previewBatchSource(activeGraphId, {
              split: split || undefined,
              n: fullSplit ? 0 : (Number(n) || 5),
              seed: Number(seed) || 42,
              step,
            });
        if (cancelled) return;
        setPreview(res);
        setPreviewError(null);
      } catch (err) {
        if (cancelled) return;
        setPreview(null);
        setPreviewError(err?.body?.detail || err.message);
      } finally {
        if (!cancelled) setPreviewing(false);
      }
    }, 350);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [graphId, source, file, split, n, fullSplit, seed, step]);

  const start = async (e) => {
    e.preventDefault();
    setStarting(true);
    setError(null);
    try {
      // Persist the canvas first so the batch runs the version on screen.
      const saved = beforeRun ? await beforeRun() : null;
      const activeGraphId = saved?.id || graphId;
      const scoring = metric ? { metric, label_key: labelKey } : {};
      const res =
        source === 'upload'
          ? await api.runBatchUpload(activeGraphId, file, { workers, ...scoring })
          : source === 'canvas'
            ? await api.runBatchCanvas(activeGraphId, { workers, ...scoring })
            : await api.runBatchSource(activeGraphId, { split: split || undefined, n: fullSplit ? 0 : (Number(n) || 5), seed: Number(seed) || 42, step, workers, ...scoring });
      // Hand the batch to the canvas and close: progress belongs on the graph,
      // not in a window covering it.
      onBatchStart?.(res.batch_id);
    } catch (err) {
      setError(err?.body?.detail || err.message);
    } finally {
      setStarting(false);
    }
  };

  const splits = Object.keys(sourceInfo?.splits || {});
  const runLabel = preview?.total ? `Run batch (${preview.total})` : 'Run batch';
  return (
    <form onClick={(e) => e.stopPropagation()} onSubmit={start}>
      <div className="field">
        <label htmlFor="data-source">Data source</label>
        <select id="data-source" value={source} onChange={(e) => setSource(e.target.value)}>
          {hasCanvasSource && <option value="canvas">Canvas source node config</option>}
          <option value="upload">Upload JSONL / CSV file</option>
          <option value="credit_risk">credit_risk feed (samples.jsonl)</option>
        </select>
      </div>
      {source === 'canvas' ? (
        <div className="muted small">
          Uses the split / n / seed / stepping configured on the canvas source node.
        </div>
      ) : source === 'upload' ? (
        <div className="field">
          <label htmlFor="batch-file">File (.jsonl or .csv; record keys map to input names)</label>
          <input id="batch-file" type="file" accept=".jsonl,.csv" required onChange={(e) => setFile(e.target.files?.[0] || null)} />
        </div>
      ) : (
        <>
          <div className="field">
            <label htmlFor="batch-split">Split</label>
            <select id="batch-split" value={split} onChange={(e) => setSplit(e.target.value)}>
              <option value="">(all)</option>
              {splits.map((s) => (
                <option key={s} value={s}>
                  {s} ({sourceInfo.splits[s]})
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>Records (n)</label>
            <input type="number" min="1" value={n} disabled={fullSplit} onChange={(e) => setN(e.target.value)} />
            <label style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 4, fontWeight: 'normal' }}>
              <input type="checkbox" checked={fullSplit} onChange={(e) => setFullSplit(e.target.checked)} />
              Entire split ({split ? (sourceInfo?.splits?.[split] ?? '?') : Object.values(sourceInfo?.splits || {}).reduce((a, b) => a + b, 0)} records)
            </label>
          </div>
          <div className="field">
            <label htmlFor="batch-step">Walk the window</label>
            <select id="batch-step" value={step} onChange={(e) => setStep(e.target.value)}>
              <option value="none">No — one run per sample, whole window at once</option>
              <option value="monthly">Monthly — one run per month of the window</option>
              <option value="weekly">Weekly — one run per week of the window</option>
              <option value="daily">Daily — one run per day of the window</option>
            </select>
            <div className="muted small">
              Each step sees only what had happened by its date, so the workflow
              accumulates instead of reading the whole window at once. It also
              multiplies the number of runs.
            </div>
          </div>
          <div className="field">
            <label htmlFor="batch-seed">Seed</label>
            <input id="batch-seed" type="number" value={seed} onChange={(e) => setSeed(e.target.value)} />
          </div>
          {sourceInfo && (
            <div className="muted small">Sample fields: {sourceInfo.fields.join(', ')}</div>
          )}
        </>
      )}
      <BatchPreview
        preview={preview}
        loading={previewing}
        error={previewError}
        idle={source === 'upload' && !file}
      />
      {error && <div className="muted small batch-error">{String(error)}</div>}
      <div className="field">
        <label>Score the results (evaluation)</label>
        <select value={metric} onChange={(e) => setMetric(e.target.value)}>
          <option value="">Don&apos;t score — just run the batch</option>
          {metrics.map((m) => (
            <option key={m.name} value={m.name}>
              {m.name}{m.custom ? ' (custom)' : ''}
            </option>
          ))}
        </select>
        <div className="muted small">
          {metric
            ? (metrics.find((m) => m.name === metric)?.description || '')
            : 'A metric compares each result with the expected answer in your data. '
              + 'Custom metrics are tools taking (prediction, label).'}
        </div>
      </div>
      {metric && (
        <div className="field">
          <label>Field holding the expected answer</label>
          <input
            value={labelKey}
            placeholder="e.g. answer, label, expected"
            required
            onChange={(e) => setLabelKey(e.target.value)}
          />
          <div className="muted small">
            Removed from each record before the run, so the workflow never sees it.
          </div>
        </div>
      )}
      <div className="field">
        <label htmlFor="batch-workers">Workers (1-5, parallel records)</label>
        <input id="batch-workers" type="number" min="1" max="5" value={workers} onChange={(e) => setWorkers(Math.max(1, Math.min(5, Number(e.target.value) || 1)))} />
      </div>
      <div className="modal-actions">
        <button type="button" onClick={onCancel} disabled={starting}>
          Cancel
        </button>
        <button type="submit" className="primary" disabled={starting || (source === 'upload' && !file)}>
          {starting ? 'Starting…' : runLabel}
        </button>
      </div>
    </form>
  );
}

export default function RunDialog({ open, graphId, workflowInputs, hasCanvasSource, onCancel, onSubmit, submitting, beforeRun, onBatchStart }) {
  const [mode, setMode] = useState('single');

  useEffect(() => {
    if (open) setMode('single');
  }, [open]);

  if (!open) return null;

  return (
    <div className="modal-backdrop" onClick={onCancel}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h3>Run workflow</h3>
        <div className="run-mode-tabs">
          <button type="button" className={mode === 'single' ? 'primary' : ''} onClick={() => setMode('single')}>
            Single run
          </button>
          <button type="button" className={mode === 'batch' ? 'primary' : ''} onClick={() => setMode('batch')}>
            Batch run
          </button>
        </div>
        {mode === 'single' ? (
          <SingleRunForm graphId={graphId} workflowInputs={workflowInputs} hasCanvasSource={hasCanvasSource} onCancel={onCancel} onSubmit={onSubmit} submitting={submitting} />
        ) : (
          <BatchRunForm graphId={graphId} hasCanvasSource={hasCanvasSource} onCancel={onCancel} beforeRun={beforeRun} onBatchStart={onBatchStart} />
        )}
      </div>
    </div>
  );
}
