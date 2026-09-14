import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../../api.js';
import { readInputFile } from './inputFiles.js';

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
  const blank = raw === '' || raw === null || raw === undefined;
  if (t === 'bool' || t === 'boolean') {
    // Three states: not provided, true, false. A checkbox could only say two,
    // so an optional flag left alone used to be sent as false.
    if (typeof raw === 'boolean') return { value: raw };
    if (blank) return { value: undefined };
    if (raw === 'true') return { value: true };
    if (raw === 'false') return { value: false };
    return { error: 'must be true or false' };
  }
  if (['int', 'integer'].includes(t)) {
    if (blank) return { value: undefined };
    if (!/^-?\d+$/.test(String(raw).trim())) return { error: 'must be a whole number' };
    const n = Number(raw);
    return Number.isSafeInteger(n) ? { value: n } : { error: 'must be a safely representable whole number' };
  }
  if (['float', 'number'].includes(t)) {
    if (blank) return { value: undefined };
    const n = Number(raw);
    if (!Number.isFinite(n)) return { error: 'must be a number' };
    return { value: n };
  }
  if (['list', 'array', 'dict', 'object', 'json'].includes(t)) {
    if (blank) return { value: undefined };
    try {
      const parsed = JSON.parse(raw);
      const wantArray = ['list', 'array'].includes(t);
      if (wantArray && !Array.isArray(parsed)) return { error: 'must be a JSON array' };
      // `null` is valid JSON and is not an object, whatever typeof says.
      if (!wantArray && t !== 'json'
          && (parsed === null || Array.isArray(parsed) || typeof parsed !== 'object')) {
        return { error: 'must be a JSON object' };
      }
      return { value: parsed };
    } catch (e) {
      return { error: `invalid JSON: ${e.message}` };
    }
  }
  // An empty string is "not provided", not a value: an optional text left
  // blank is left out of the request rather than sent as "".
  return { value: blank ? undefined : raw };
}

export function defaultFor(type, required = true) {
  const t = String(type || 'str').toLowerCase();
  if (t === 'bool' || t === 'boolean') return required ? 'false' : '';
  if (['list', 'array'].includes(t)) return '[]';
  if (['dict', 'object', 'json'].includes(t)) return '{}';
  return '';
}

export function toField(type, value) {
  // The form edits text (or a select); a prefilled value arrives in its real
  // type, so structured values come back as the JSON the field expects.
  const t = String(type || 'str').toLowerCase();
  if (t === 'bool' || t === 'boolean') {
    return value === null || value === undefined ? '' : String(!!value);
  }
  if (value === null || value === undefined) return defaultFor(type);
  return typeof value === 'string' ? value : JSON.stringify(value, null, 2);
}

// Where the last-used inputs are kept: per workflow *revision* and start
// point, so a re-shaped flow does not get an older flow's values back.
function inputsKey(graphId, plan, startAt) {
  return `${INPUTS_KEY}:${graphId}:${plan?.graph_revision || ''}:${startAt || ''}`;
}

function SingleRunForm({ graphId, onCancel, onSubmit, submitting, beforeRun, onSwitchMode }) {
  const [values, setValues] = useState({});
  const [touched, setTouched] = useState(false);
  const [fileError, setFileError] = useState(null);
  const [fileBusy, setFileBusy] = useState(false);
  const [fileNotice, setFileNotice] = useState(null);
  const fileSeq = useRef(0);
  // Starting part-way through: the nodes before the start are left out and
  // their outputs become inputs, usually taken from an earlier run.
  const [startAt, setStartAt] = useState('');
  // Runs sharing a session are reminded of what the others did, in order.
  // Blank means this run stands alone, which is what most runs should be.
  const [session, setSession] = useState('');
  // The plan is the only thing this form reads: schema, start points,
  // warnings and prefill all come from one revision of the graph, and the
  // run request presents the plan's id so the server can hold it to that.
  const [plan, setPlan] = useState(null);
  const [planning, setPlanning] = useState(true);
  const [planError, setPlanError] = useState(null);
  const [launchError, setLaunchError] = useState(null);
  // A source that yields several records: which one this run takes has to be
  // said. '' = not yet said, 'first', or 'choose' with an index.
  const [recordChoice, setRecordChoice] = useState('');
  const [recordIndex, setRecordIndex] = useState('');
  const [records, setRecords] = useState(null);
  // Held in a ref so re-rendering the parent does not re-plan.
  const beforeRunRef = useRef(beforeRun);
  beforeRunRef.current = beforeRun;
  // Each request carries a token; an answer to an older request is dropped,
  // so switching Start from twice cannot end on the first answer.
  const planSeq = useRef(0);

  const requestPlan = useCallback(async () => {
    if (!graphId) return;
    const token = ++planSeq.current;
    setPlanning(true);
    try {
      // Planned from what is on screen: the canvas is saved first, else the
      // plan would describe the last saved version, not the one being run.
      const saved = await beforeRunRef.current?.();
      const found = await api.runPlan(saved?.id || graphId, { startAt: startAt ? [startAt] : [] });
      if (token !== planSeq.current) return;
      setPlan(found);
      setPlanError(null);
      setRecords(null);
      setRecordChoice('');
      setRecordIndex('');
    } catch (err) {
      if (token !== planSeq.current) return;
      setPlan(null);
      const detail = err?.body?.detail;
      setPlanError(Array.isArray(detail) ? detail.join(' ') : (detail || err.message));
    } finally {
      if (token === planSeq.current) setPlanning(false);
    }
  }, [graphId, startAt]);

  useEffect(() => { requestPlan(); }, [requestPlan]);

  const fields = useMemo(() => plan?.inputs || [], [plan]);

  useEffect(() => {
    // Re-running while iterating is the common case, so the last values used
    // for this workflow come back pre-filled; history supplies missing values.
    let saved = {};
    try {
      saved = JSON.parse(localStorage.getItem(inputsKey(graphId, plan, startAt))) || {};
    } catch {
      saved = {};
    }
    const prefill = plan?.prefill || {};
    const init = {};
    fields.forEach((w) => {
      const source = saved[w.name] !== undefined ? saved[w.name] : prefill[w.name];
      init[w.name] = source !== undefined
        ? toField(w.type, source) : defaultFor(w.type, w.required);
    });
    setValues(init);
    setTouched(false);
  }, [fields, plan, graphId, startAt]);

  useEffect(() => { fileSeq.current += 1; setFileBusy(false); setFileError(null); setFileNotice(null); }, [plan, graphId]);
  useEffect(() => () => { fileSeq.current += 1; }, []);
  async function importFile(name, type, file) {
    const token = ++fileSeq.current;
    setFileBusy(true); setFileError(null); setFileNotice(null);
    try {
      const text = await readInputFile(file);
      if (token !== fileSeq.current) return;
      const parsed = parseValue(type, text);
      if (parsed.error) throw new Error(`${name}: ${parsed.error}`);
      setValues(v => ({ ...v, [name]: text }));
      setFileNotice(`${file.name} loaded into ${name} (${text.length.toLocaleString()} characters). Review before running.`);
    } catch (e) { if (token === fileSeq.current) setFileError(e.message); }
    finally { if (token === fileSeq.current) setFileBusy(false); }
  }

  const parsed = fields.map((w) => ({ input: w, ...parseValue(w.type, values[w.name]) }));
  const errors = parsed.filter((p) => p.error);
  const missing = parsed.filter(
    (p) => p.input.required && !p.error && (p.value === undefined || p.value === '')
  );

  const submit = async (e) => {
    e.preventDefault();
    setTouched(true);
    setLaunchError(null);
    if (fileBusy || fileError || submitting || planning || planError || !plan || !recordSettled || errors.length || missing.length) return;
    const payload = {};
    parsed.forEach((p) => {
      if (p.value !== undefined) payload[p.input.name] = p.value;
    });
    try {
      localStorage.setItem(inputsKey(graphId, plan, startAt), JSON.stringify(values));
    } catch {
      /* storage unavailable; the run still proceeds */
    }
    const outcome = await onSubmit(
      payload, startAt ? [startAt] : undefined, session.trim() || undefined, plan.plan_id,
      record);
    // A refusal is read here, with the inputs still in place. A stale plan
    // means the workflow changed underneath us: plan again rather than run
    // the newer version unseen.
    if (outcome && !outcome.ok) {
      setLaunchError(outcome.error || 'The run did not start.');
      if (outcome.stale) requestPlan();
    }
  };

  const set = (name, value) => { fileSeq.current += 1; setFileBusy(false); setFileError(null); setFileNotice(null); setValues((v) => ({ ...v, [name]: value })); };
  const startPoints = plan?.start_points || [];
  const cardinality = plan?.source?.cardinality;
  const mustChoose = !!plan?.source && (cardinality == null || cardinality > 1);
  // Which record this run takes; undefined when the source yields one, or none.
  const record = !mustChoose ? undefined
    : recordChoice === 'first' ? 0
      : recordChoice === 'choose' && recordIndex !== '' ? Number(recordIndex) : undefined;
  const recordSettled = !mustChoose || record !== undefined;

  const loadRecords = async () => {
    setRecordChoice('choose');
    if (records) return;
    const token = planSeq.current;
    try {
      const found = await api.runPlan(plan.graph_id || graphId, { startAt: startAt ? [startAt] : [], includeRecords: true });
      if (token !== planSeq.current) return;
      if (found.plan_id !== plan.plan_id) {
        setLaunchError('The workflow changed. Review the updated plan and choose a record again.');
        requestPlan();
        return;
      }
      setRecords(found.records || []);
      setPlanError(null);
    } catch (err) {
      if (token === planSeq.current) setPlanError(err?.body?.detail || err.message);
    }
  };

  return (
    <form onClick={(e) => e.stopPropagation()} onSubmit={submit}>
      {(startAt || startPoints.length > 0) && (
        <div className="field">
          <label htmlFor="run-start-at">Start from</label>
          <select id="run-start-at" value={startAt} onChange={(e) => setStartAt(e.target.value)}>
            <option value="">The beginning (whole workflow)</option>
            {startPoints.map((name) => (
              <option key={name} value={name}>
                {name} and everything after it
              </option>
            ))}
          </select>
          {startAt && plan && (
            <div className="muted small" data-testid="start-from-summary">
              Runs: {plan.nodes.map((n) => n.name).join(', ')}.
              {' '}Skipped: {(plan.skipped || []).join(', ') || 'nothing'}.
              {(plan.prefill_missing || []).length > 0 && (
                <> Needs your input: {plan.prefill_missing.join(', ')}.</>
              )}
            </div>
          )}
        </div>
      )}
      {!planning && plan && (
        <section className="batch-preview" aria-label="Run summary">
          <strong>Single run · 1 record</strong>
          <div>Starts at: {plan.start_at?.join(', ') || 'The beginning'}</div>
          <div>Participating nodes: {plan.nodes.map((node) => node.name).join(', ')}</div>
          {!!plan.skipped?.length && <div>Excluded nodes: {plan.skipped.join(', ')}</div>}
          <div>Data: {plan.source ? `Canvas source “${plan.source.node}”`
            : fields.length ? 'Inputs below' : 'No external inputs'}</div>
          {plan.source && <div>{record !== undefined
            ? `This run uses record #${record + 1} only.`
            : mustChoose ? 'Choose one record below before running.' : 'This run uses the first record only.'}</div>}
          <div role="status">{missing.length
            ? `Missing inputs: ${missing.map((p) => p.input.name).join(', ')}`
            : errors.length ? `Check input format: ${errors.map((p) => p.input.name).join(', ')}`
              : 'Required inputs are ready.'}</div>
        </section>
      )}
      {!planning && mustChoose && (
        <div className="field batch-preview">
          <div>
            Source <span className="tool-option-name">{plan.source.node}</span> yields{' '}
            {cardinality == null ? 'an unknown number of' : cardinality} records — which one does this run take?
          </div>
          <label className="param-req">
            <input type="radio" name="run-record" checked={recordChoice === 'first'}
              onChange={() => setRecordChoice('first')} />
            Run the first record
          </label>
          <label className="param-req">
            <input type="radio" name="run-record" checked={recordChoice === 'choose'}
              onChange={loadRecords} />
            Choose a record
          </label>
          {recordChoice === 'choose' && (
            <select aria-label="Record" value={recordIndex}
              onChange={(e) => setRecordIndex(e.target.value)}>
              <option value="">{records ? 'Pick one…' : 'Loading records…'}</option>
              {(records || []).map((r) => (
                <option key={r.index} value={r.index}>
                  {`#${r.index + 1} `}
                  {Object.entries(r.summary || {}).map(([k, v]) => `${k}: ${v}`).join(' · ')}
                </option>
              ))}
            </select>
          )}
          <div>
            <button type="button" onClick={() => onSwitchMode?.('batch')}>
              {cardinality == null ? 'Run all records as a batch' : `Run all ${cardinality} as a batch`}
            </button>
          </div>
        </div>
      )}
      <div className="field">
        <label htmlFor="run-session">
          Session <span className="muted">(optional)</span>
        </label>
        <input
          id="run-session"
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
          History fallback from run <span className="tool-option-name">{plan.prefill_from_run}</span>:{' '}
          {Object.keys(plan.prefill).join(', ')}
        </div>
      )}
      {(plan?.warnings || []).filter(() => !mustChoose).map((w) => (
        <div key={w} className="muted small batch-preview">{w}</div>
      ))}
      {plan?.source && (
        <p className="muted small">Inputs come from the canvas source node; sampled at run time.</p>
      )}
      {planning && <p className="muted small">Working out what this run needs…</p>}
      {planError && <div className="chat-error">Cannot plan this run: {String(planError)}</div>}
      {!planning && plan && fields.length === 0 && !plan.source && (
        <p className="muted">This workflow has no external inputs.</p>
      )}
      {parsed.map(({ input: w, error }) => {
        const type = String(w.type || 'str').toLowerCase();
        const isBool = type === 'bool' || type === 'boolean';
        const isNumber = ['int', 'integer', 'float', 'number'].includes(type);
        const isJson = ['list', 'array', 'dict', 'object', 'json'].includes(type);
        const id = `run-input-${w.name}`;
        return (
          <div className="field" key={w.name}>
            <label htmlFor={id}>
              {w.name}{' '}
              <span className="muted">
                ({w.type || 'str'}
                {w.required ? ', required' : ''})
              </span>
              {w.consumed_by && <span className="muted small"> → {w.consumed_by}</span>}
            </label>
            {w.description && <div className="muted small">{w.description}</div>}
            {isBool ? (
              <select id={id} value={values[w.name] ?? ''} onChange={(e) => set(w.name, e.target.value)}>
                {!w.required && <option value="">not provided</option>}
                <option value="true">true</option>
                <option value="false">false</option>
              </select>
            ) : isNumber ? (
              <input
                id={id}
                type="number"
                step={['int', 'integer'].includes(type) ? '1' : 'any'}
                value={values[w.name] ?? ''}
                onChange={(e) => set(w.name, e.target.value)}
              />
            ) : (
              <textarea
                id={id}
                className={isJson ? 'code-input' : undefined}
                rows={isJson ? 3 : 2}
                value={values[w.name] ?? ''}
                onChange={(e) => set(w.name, e.target.value)}
              />
            )}
            {!isNumber && !isBool && <div className="muted small">
              <label>Load file into {w.name} (replaces this field)
                <input type="file" aria-label={`Load file into ${w.name}`} accept=".txt,.md,.json,.csv,.tsv,.log" disabled={fileBusy || submitting}
                  onChange={e => { const file = e.target.files?.[0]; e.target.value = ''; if (file) importFile(w.name, type, file); }} />
              </label>
              UTF-8 text, up to 1 MB. Read locally; sent only when you run.
            </div>}
            {touched && error && <div className="chat-error">{w.name} {error}</div>}
          </div>
        );
      })}
      {touched && missing.length > 0 && (
        <div className="chat-error">Required: {missing.map((p) => p.input.name).join(', ')}</div>
      )}
      {fileBusy && <div role="status">Reading file…</div>}
      {fileError && <div role="alert" className="chat-error">{fileError}</div>}
      {fileNotice && <div role="status" className="muted small">{fileNotice}</div>}
      {launchError && <div className="chat-error">{String(launchError)}</div>}
      <div className="modal-actions">
        <button type="button" onClick={onCancel} disabled={submitting}>
          Cancel
        </button>
        <button type="submit" className="primary"
          disabled={fileBusy || !!fileError || submitting || planning || !!planError || !plan || !recordSettled}>
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
  if (!preview || preview.requires_collection) return null;
  return (
    <div className={`batch-preview${preview.total >= BIG_BATCH ? ' batch-preview-big' : ''}`}>
      <strong>{previewSummary(preview)}</strong>
      {loading && <div role="status">Updating preview… Wait for the latest count before running.</div>}
      <details className="input-disclosure"><summary>Run details</summary>
      {preview.nodes?.length > 0 && <div>Participating nodes: {preview.nodes.map((node) => node.name).join(', ')}</div>}
      {!!preview.skipped?.length && <div>Excluded nodes: {preview.skipped.join(', ')}</div>}
      {preview.node_count > 0 && (
        <div className="muted small">
          {`${preview.total} records × ${preview.node_count} nodes = up to `}
          {`${preview.node_executions ?? preview.total * preview.node_count} node executions`}
        </div>
      )}
      <div className="muted small">
        Stopping a batch spares the records not yet started; those already running will finish.
      </div>
      {preview.total >= BIG_BATCH && (
        <div className="muted small">
          Each run is a full pass of the workflow. This one is large.
        </div>
      )}
      {preview.fields?.length > 0 && (
        <div className="muted small">Each run gets: {preview.fields.join(', ')}</div>
      )}
      </details>
    </div>
  );
}

function BatchRunForm({ graphId, hasCanvasSource, onCancel, beforeRun, onBatchStart }) {
  // A graph with a source node on it has already said where its data comes
  // from; making you pick a file again is the wrong default.
  const [source, setSource] = useState(hasCanvasSource ? 'canvas' : 'upload');
  const [dataset, setDataset] = useState('contemporary');
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
  const [apiBatch, setApiBatch] = useState(false);
  const [apiBatchSize, setApiBatchSize] = useState(32);
  const executionParams = apiBatch ? {llm_batch_size: Number(apiBatchSize)} : {workers};
  const invalidApiBatch = apiBatch && (!Number.isInteger(Number(apiBatchSize)) || Number(apiBatchSize) < 1 || Number(apiBatchSize) > 1024);
  // An evaluation is this same batch with a metric attached; leaving the metric
  // empty runs it as an ordinary batch.
  const [metric, setMetric] = useState('');
  const [labelKey, setLabelKey] = useState('');
  const [metrics, setMetrics] = useState([]);
  const [preview, setPreview] = useState(null);
  const [previewing, setPreviewing] = useState(false);
  const [previewError, setPreviewError] = useState(null);
  const [collection, setCollection] = useState(() => {
    try { const id = localStorage.getItem(`source-collection:${graphId}`); return id ? { id, status: 'collecting' } : null; } catch { return null; }
  });
  const [collectionSource, setCollectionSource] = useState(null);
  const [stoppingCollection, setStoppingCollection] = useState(false);
  const [collecting, setCollecting] = useState(false);
  const [collectionOptions, setCollectionOptions] = useState({});
  const [collectionMode, setCollectionMode] = useState('all');
  const [preprocessors, setPreprocessors] = useState([]);
  useEffect(() => {
    api.listCustomTools().then(r => setPreprocessors((r.tools || []).filter(t => (t.params || []).length === 1))).catch(() => setPreprocessors([]));
  }, []);
  const [chunkSize, setChunkSize] = useState(10);
  const [collectionGraph, setCollectionGraph] = useState(graphId);
  useEffect(() => {
    if (!collection?.id || collection.status !== 'collecting') return;
    setCollecting(true);
    let active = true;
    let polling = false;
    const timer = setInterval(async () => {
      if (polling) return;
      polling = true;
      try {
        const value = await api.sourceCollection(collectionGraph, collection.id);
        if (!active) return;
        setCollection(value);
        if (value.source) setCollectionSource(value.source);
        if (value.mode) setCollectionMode(value.mode);
        if (value.batch_size) setChunkSize(value.batch_size);
        if (value.status !== 'collecting') {
          setCollecting(false); setStoppingCollection(false);
          if (['failed', 'cancelled'].includes(value.status)) setError(value.error);
        }
      } catch (err) { if (active) { setError(err.body?.detail || err.message); setCollecting(false); } }
      finally { polling = false; }
    }, 1000);
    return () => { active = false; clearInterval(timer); };
  }, [collection?.id, collection?.status, collectionGraph]);
  async function stopCollection() {
    setStoppingCollection(true);
    try { await api.stopSourceCollection(collectionGraph, collection.id); }
    catch (err) { setError(err.body?.detail || err.message); setStoppingCollection(false); }
  }
  async function collect() {
    setCollecting(true); setStoppingCollection(false); setError(null); setCollection(null);
    try {
      const saved = await beforeRunRef.current?.();
      const id = saved?.id || graphId;
      setCollectionGraph(id);
      const value = await api.collectSource(id, { ...collectionOptions, mode: collectionMode, batch_size: Number(chunkSize), ...executionParams, ...(metric ? { metric, label_key: labelKey } : {}) });
      setCollection(value);
      try { localStorage.setItem(`source-collection:${id}`, value.id); } catch { /* Storage can be unavailable. */ }
      if (value.status !== 'collecting') {
        setCollecting(false);
        if (value.status === 'failed') setError(value.error);
      }
    } catch (err) { setError(err.body?.detail || err.message); setCollecting(false); }
  }
  const collectionId = collection?.status === 'ready' ? collection.id : undefined;


  useEffect(() => {
    let active = true;
    api.creditRiskSource(dataset).then(info => { if (active) { setSourceInfo(info); if (info.dataset && info.dataset !== dataset) setDataset(info.dataset); } }).catch(() => { if (active) setSourceInfo(null); });
    api.listMetrics().then((r) => setMetrics(r.metrics || [])).catch(() => setMetrics([]));
    return () => { active = false; };
  }, [dataset]);

  // Held in a ref so re-rendering the parent does not re-run the preview.
  const beforeRunRef = useRef(beforeRun);
  beforeRunRef.current = beforeRun;

  // Ask the server what this configuration would actually run. It resolves the
  // records the same way the batch does, so the count is the real one.
  useEffect(() => {
    if ((source === 'canvas' && collectionMode === 'prepare') || (source === 'upload' && !file)) {
      setPreview(null);
      setPreviewError(null);
      setPreviewing(false);
      return undefined;
    }
    let cancelled = false;
    setPreviewing(true);
    const timer = setTimeout(async () => {
      try {
        // The canvas source node's config lives in the graph, so the saved
        // version has to match the screen for the count to be true.
        const saved = await beforeRunRef.current?.();
        const activeGraphId = saved?.id || graphId;
        // With the scoring and workers it will run with, so a label field
        // that is missing is found here, not eighteen runs later.
        const scoring = metric ? { metric, label_key: labelKey } : {};
        const res = source === 'upload'
          ? await api.previewBatchUpload(activeGraphId, file, { ...executionParams, ...scoring })
          : source === 'canvas'
            ? await api.previewBatchCanvas(activeGraphId, { ...executionParams, ...scoring, ...(collectionId ? { collection_id: collectionId } : {}) })
            : await api.previewBatchSource(activeGraphId, {
              ...(dataset !== 'contemporary' ? { dataset } : {}),
              split: split || undefined,
              n: fullSplit ? 0 : (Number(n) || 5),
              seed: Number(seed) || 42,
              step,
              ...executionParams,
              ...scoring,
            });
        if (cancelled) return;
        setPreview(res);
        if (res.requires_collection) setCollectionSource(res.source);
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
  }, [graphId, source, file, split, n, fullSplit, seed, step, metric, labelKey, workers, collectionId, dataset, collectionMode, apiBatch, apiBatchSize]);

  const start = async (e) => {
    e.preventDefault();
    if (!canStart) return;
    setStarting(true);
    setError(null);
    try {
      // Persist the canvas first so the batch runs the version on screen.
      const saved = beforeRun ? await beforeRun() : null;
      const activeGraphId = saved?.id || graphId;
      const scoring = metric ? { metric, label_key: labelKey } : {};
      const res =
        source === 'upload'
          ? await api.runBatchUpload(activeGraphId, file, { ...executionParams, ...scoring })
          : source === 'canvas'
            ? await api.runBatchCanvas(activeGraphId, { ...executionParams, ...scoring, ...(collectionId ? { collection_id: collectionId } : {}) })
            : await api.runBatchSource(activeGraphId, { ...(dataset !== 'contemporary' ? { dataset } : {}), split: split || undefined, n: fullSplit ? 0 : (Number(n) || 5), seed: Number(seed) || 42, step, ...executionParams, ...scoring });
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
  // Records from the credit-risk feed carry the sample they came from, and
  // with it the truth; the preview says so through the fields it lists.
  const selfLabelled = (preview?.fields || []).includes('sample_json');
  const runLabel = preview?.total ? `Run batch (${preview.total})` : 'Run batch';
  // Nothing starts on a count that has not arrived, failed, or is zero.
  const canStart = !invalidApiBatch && !(source === 'canvas' && collectionMode !== 'all') && !!preview?.total && !previewing && !previewError && !starting && !collecting && !(source === 'canvas' && collectionSource && !collectionId);
  const needsCollection = source === 'canvas' && (collectionMode !== 'all' || (!!preview?.requires_collection && !collectionId));
  const collectionInvalid = invalidApiBatch || collecting || starting || (collectionMode === 'prepare' && !collectionOptions.preprocess_tool)
    || (collectionMode !== 'all' && (!Number.isInteger(Number(chunkSize)) || Number(chunkSize) < 1 || Number(chunkSize) > 1000));
  const collectionLabel = collecting ? 'In progress…' : collectionMode === 'prepare'
    ? 'Preprocess all and run batches' : collectionMode === 'stream' ? 'Start collecting and running' : 'Collect data';
  return (
    <form onClick={(e) => e.stopPropagation()} onSubmit={start}>
      <details className="input-disclosure" open={!hasCanvasSource}>
        <summary>{source === 'canvas' ? 'Using canvas inputs · change source' : 'Input source'}</summary>
      <div className="field">
        <label htmlFor="data-source">Data source</label>
        <select id="data-source" value={source} onChange={(e) => setSource(e.target.value)}>
          {hasCanvasSource && <option value="canvas">Canvas source node config</option>}
          <option value="upload">Upload JSON / JSONL / CSV file</option>
          <option value="credit_risk">credit_risk feed (samples.jsonl)</option>
        </select>
      </div>
      </details>
      {source === 'canvas' ? (
        <div className="muted small">

          {<div className="field">
            <label>Execution mode<select disabled={collecting || starting} value={collectionMode} onChange={e => { setCollectionMode(e.target.value); setCollection(null); }}>
              <option value="all">Use all available data</option>
              <option value="stream">Run in batches as data arrives</option>
              <option value="prepare">Clean all data first, then run batches</option>
            </select></label>
            {collectionMode !== 'all' && <>
              <label>Records per batch<input type="number" min="1" max="1000" disabled={collecting} value={chunkSize} onChange={e => setChunkSize(e.target.value)} /></label>
              <p>{collectionMode === 'prepare' ? 'Clean the complete dataset once, then run it in batches.' : 'Start as each batch fills. The final batch includes any remaining records.'}</p>
            </>}
            {collectionMode === 'prepare' && <label>Whole-dataset preprocessor
              <select disabled={collecting} value={collectionOptions.preprocess_tool || ''} onChange={e => setCollectionOptions(v => ({...v, preprocess_tool:e.target.value}))}>
                <option value="">Choose a list-to-list custom tool</option>
                {preprocessors.map(t => <option key={t.name} value={t.name}>{t.name}</option>)}
              </select>
              <p>Choose a saved cleanup tool from Library → Custom. It receives the full dataset, including shared lists.</p>
            </label>}

            {collectionSource?.type === 'gdelt_news' && <details className="input-disclosure"><summary>Date range & collection settings</summary>
              <p>Collects news titles and links. Empty windows are skipped.</p>
              <p>{collectionSource.query} · leave dates blank to use the source’s configured range or days back.</p>
              <label>Collection start date<input type="date" disabled={collecting} value={collectionOptions.start_date ?? collectionSource.start_date ?? ''} onChange={e => { setCollection(null); setCollectionOptions(v => ({ ...v, start_date: e.target.value })); }} /></label>
              <label>Collection end date<input type="date" disabled={collecting} value={collectionOptions.end_date ?? collectionSource.end_date ?? ''} onChange={e => { setCollection(null); setCollectionOptions(v => ({ ...v, end_date: e.target.value })); }} /></label>
              <label>One run per<select disabled={collecting} value={collectionOptions.batch_step ?? collectionSource.batch_step ?? 'daily'} onChange={e => { setCollection(null); setCollectionOptions(v => ({ ...v, batch_step: e.target.value })); }}>
                <option value="daily">Day</option><option value="weekly">Week</option><option value="monthly">Month</option>
              </select></label>
            </details>}
            {collecting && <p role="status">{collection?.phase === 'preprocessing' ? 'Preprocessing the entire dataset; workflow has not started' : collection?.collection_complete ? 'Collection complete; finishing batches' : 'Collecting input data'}{collection?.total ? `: ${collection.completed} / ${collection.total} windows` : '…'} · {['stream', 'prepare'].includes(collection?.mode) ? `${collection.record_count || 0} records collected, ${collection.submitted_records || 0} submitted to batches` : 'Workflow has not started.'}</p>}
            {['stream', 'prepare'].includes(collection?.mode) && <>
              {collection.status === 'completed' && <p role="status">Collection and all batches completed · {collection.record_count} input records{collection.processed_count != null ? ` → ${collection.processed_count} processed records` : ''}.</p>}
              {(collection.batches || []).map((item, index) => <div key={item.id}>Batch {index + 1} · {item.total} records · {item.status || 'running'} <button type="button" onClick={() => onBatchStart?.(item.id)}>View batch {index + 1}</button></div>)}
            </>}
            {collectionId && <>
              <p role="status">Collection complete · {collection.record_count} saved records. Ready for batch run.</p>
              <details><summary>Preview collected inputs</summary><pre style={{ maxHeight: 240, overflow: 'auto', whiteSpace: 'pre-wrap' }}>{JSON.stringify(collection.sample, null, 2)}</pre></details>
              <button type="button" className="link small" disabled={collecting} onClick={collect}>Refresh collected data</button>
            </>}
          </div>}
        </div>
      ) : source === 'upload' ? (
        <div className="field">
          <label htmlFor="batch-file">File (.jsonl or .csv; record keys map to input names)</label>
          <input id="batch-file" type="file" accept=".json,.jsonl,.csv" required onChange={(e) => setFile(e.target.files?.[0] || null)} />
        </div>
      ) : (
        <>
          <div className="field"><label htmlFor="batch-dataset">Dataset version</label>
            <select id="batch-dataset" value={dataset} onChange={e => { setDataset(e.target.value); setSplit('dev'); }}>
              {(sourceInfo?.datasets || [{id:'contemporary',label:'Contemporary (legacy)'}]).map(d => <option key={d.id} value={d.id}>{d.label}</option>)}
            </select>
            {dataset !== 'contemporary' && <p>Counts select trajectories. Walk the window controls how its observations are grouped.</p>}
          </div>
          <div className="field">
            <label htmlFor="batch-split">Split</label>
            <select id="batch-split" value={split} onChange={(e) => setSplit(e.target.value)}>
              <option value="">(all)</option>
              {splits.map((s) => (
                <option key={s} value={s}>
                  {s} ({sourceInfo.splits[s]} {dataset === 'contemporary' ? 'samples' : 'trajectories'})
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
              {dataset === 'contemporary'
                ? 'Each step sees the evidence available up to its date.'
                : 'Each step contains new evidence within that period. Weekly periods start at the window start; monthly periods end at the calendar month end. Empty periods are skipped.'}
              {' '}The preview shows the resulting number of runs.
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
      <details className="input-disclosure"><summary>Evaluation · {metric || 'off'}</summary>
      <div className="field">
        <label htmlFor="batch-metric">Score the results (evaluation)</label>
        <select id="batch-metric" disabled={collecting} value={metric} onChange={(e) => setMetric(e.target.value)}>
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
            : 'Optional: any finished batch can be evaluated afterwards from its Evaluation tab.'}
        </div>
      </div>
      {metric && selfLabelled && (
        <div className="muted small">
          The credit-risk feed carries its own truth; nothing to name. You can
          also skip this and press Evaluate on the finished batch.
        </div>
      )}
      {metric && !selfLabelled && (
        <div className="field">
          <label htmlFor="batch-label-key">Field holding the expected answer</label>
          <input
            id="batch-label-key"
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
      </details>
      <div className="field">
        <label htmlFor="model-execution">Model execution</label>
        <select id="model-execution" disabled={collecting || starting} value={apiBatch ? 'native' : 'standard'} onChange={e => setApiBatch(e.target.value === 'native')}>
          <option value="standard">Standard API</option>
          <option value="native">SafeChain / native batch API</option>
        </select>
      </div>
      {apiBatch && <div className="field">
        <label htmlFor="api-batch-size">API batch size</label>
        <input id="api-batch-size" type="number" min="1" max="1024" disabled={collecting || starting} value={apiBatchSize} onChange={e => setApiBatchSize(e.target.value)} />
        <p className="muted small">Maximum model requests per SafeChain batch. Different from records per workflow batch.</p>
        {invalidApiBatch && <p role="alert">Enter a whole number from 1 to 1024.</p>}
      </div>}
      {!apiBatch && <details className="input-disclosure"><summary>Parallel runs · {workers} workers</summary>
      <div className="field">
        <label htmlFor="batch-workers">Workers (records run at the same time)</label>
        <div className="workers-row">
          <input id="batch-workers" type="number" min="1" max="64" value={workers} onChange={(e) => setWorkers(Math.max(1, Math.min(64, Number(e.target.value) || 1)))} />
          {preview?.samples > 0 && preview.samples !== workers && (
            <button type="button" className="link small" onClick={() => setWorkers(Math.min(64, preview.samples))}>
              one per sample ({preview.samples})
            </button>
          )}
        </div>
        <div className="muted small">
          A sample's steps run one after another, so more workers than samples does nothing.
        </div>
      </div>
      </details>}
      <div className="modal-actions">
        <button type="button" onClick={onCancel} disabled={starting}>
          Cancel
        </button>
        {collecting && collection?.id && <button type="button" disabled={stoppingCollection} onClick={stopCollection}>
          {stoppingCollection ? 'Stopping…' : ['stream', 'prepare'].includes(collection?.mode) ? 'Stop collection and runs' : 'Stop collection'}
        </button>}
        {needsCollection ? <button type="button" className="primary" disabled={collectionInvalid} onClick={collect}>{collectionLabel}</button>
          : <button type="submit" className="primary" disabled={!canStart}>{starting ? 'Starting…' : runLabel}</button>}
      </div>
    </form>
  );
}

export default function RunDialog({ open, graphId, hasCanvasSource, onCancel, onSubmit, submitting, beforeRun, onBatchStart }) {
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
          <SingleRunForm graphId={graphId} hasCanvasSource={hasCanvasSource} onCancel={onCancel} onSubmit={onSubmit} submitting={submitting} beforeRun={beforeRun} onSwitchMode={setMode} />
        ) : (
          <BatchRunForm graphId={graphId} hasCanvasSource={hasCanvasSource} onCancel={onCancel} beforeRun={beforeRun} onBatchStart={onBatchStart} />
        )}
      </div>
    </div>
  );
}
