import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import NumberInput from '../../components/NumberInput.jsx';
import { api } from '../../api.js';
import PythonDatasetEditor from '../data/PythonDatasetEditor.jsx';
import EvaluatorParams from './EvaluatorParams.jsx';
import EvaluatorReports from './EvaluatorReports.jsx';
import { clearLocalDraft, newerDraft, readLocalDraft, writeLocalDraft } from './draftStore.js';

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

export const SIMPLE_EXAMPLE = `def evaluate(records, field: str = "result"):
    """Score saved results; every parameter below becomes a form field."""
    values = [record.get("outputs", {}).get(field) for record in records]
    filled = [v for v in values if v not in (None, "")]
    return {
        "metrics": {"filled_rate": len(filled) / len(values) if values else None},
        "coverage": {"unit": "records", "total": len(values), "scored": len(filled),
                     "unscored": len(values) - len(filled)},
    }
`;

const EXAMPLES = [
  { label: 'Factory with parameters', code: EVALUATOR_EXAMPLE },
  { label: 'Plain evaluate function', code: SIMPLE_EXAMPLE },
];

const DRAFT_DEBOUNCE_MS = 800;
const message = (e) => e?.body?.detail || e?.message || String(e);
const rows = (value, key) => (Array.isArray(value) ? value : value?.[key] || []);
// A preview or run answers with the report, either bare or wrapped.
const reportOf = (result) => result?.report || result;
const metricNames = (result) => {
  if (!result) return [];
  if (Array.isArray(result.metrics)) return result.metrics.map((m) => m.name).filter(Boolean);
  const metrics = reportOf(result)?.metrics;
  return metrics && typeof metrics === 'object' ? Object.keys(metrics) : [];
};

/**
 * Evaluation as code: write it here, read its parameters from the code
 * itself, try it on a saved batch or run, and keep the ones worth keeping on
 * the workflow. Nothing about evaluation lives on the canvas.
 */
export default function EvaluatePanel({ graphId, onEvaluatorsChanged }) {
  const [code, setCode] = useState('');            // confirmed code: what runs
  const [live, setLive] = useState('');            // what is in the editor, unconfirmed included
  const [config, setConfig] = useState({});
  const [iface, setIface] = useState(null);
  const [checking, setChecking] = useState(false);
  const [targets, setTargets] = useState([]);
  const [selection, setSelection] = useState('');
  const [reload, setReload] = useState(0);
  const [report, setReport] = useState(null);
  const [reportLabel, setReportLabel] = useState('preview');
  const [metric, setMetric] = useState('');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [saved, setSaved] = useState([]);
  const [resources, setResources] = useState([]);
  const [name, setName] = useState('');
  const [timing, setTiming] = useState('manual');
  const [timeout, setTimeoutSeconds] = useState(120);
  const [direction, setDirection] = useState('maximize');
  const [labels, setLabels] = useState('');
  const [restored, setRestored] = useState(false);
  const [draftState, setDraftState] = useState('idle');
  const [draftSavedAt, setDraftSavedAt] = useState(null);
  const baseline = useRef({ code: '', config: {} });
  const pending = useRef({ code: '', config: {} });
  const unsaved = useRef(false);

  useEffect(() => { api.listDataResources().then((r) => setResources(rows(r, 'resources'))).catch(() => {}); }, []);

  // ---- saved evaluators (they live on the workflow, not on a node) ----
  const refreshSaved = useCallback(async () => {
    if (!graphId) return;
    try { setSaved(rows(await api.graphEvaluators(graphId), 'evaluators')); }
    catch (e) { setError(message(e)); }
  }, [graphId]);
  useEffect(() => { refreshSaved(); }, [refreshSaved]);

  // ---- the draft: server copy wins, browser copy is the safety net ----
  useEffect(() => {
    let active = true;
    setRestored(false); setCode(''); setConfig({}); setIface(null); setReport(null); setMetric('');
    baseline.current = { code: '', config: {} }; unsaved.current = false;
    if (!graphId) return undefined;
    (async () => {
      let server = null;
      try { server = await api.evaluatorDraft(graphId); } catch { server = null; }
      if (!active) return;
      const draft = newerDraft(server, readLocalDraft(graphId)) || {};
      setCode(draft.code || ''); setConfig(draft.config || {}); setDraftSavedAt(draft.saved_at || null);
      setDraftState(draft.code ? 'saved' : 'idle');
      baseline.current = { code: draft.code || '', config: draft.config || {} };
      setRestored(true);
    })();
    return () => { active = false; };
  }, [graphId]);

  const saveDraft = useCallback(async (draft) => {
    writeLocalDraft(graphId, draft);
    setDraftState('saving');
    try {
      await api.saveEvaluatorDraft(graphId, draft);
      baseline.current = draft; unsaved.current = false;
      setDraftSavedAt(new Date().toISOString()); setDraftState('saved');
    } catch { setDraftState('local'); }
  }, [graphId]);

  useEffect(() => { pending.current = { code: live, config }; }, [live, config]);

  useEffect(() => {
    if (!graphId || !restored) return undefined;
    const same = live === baseline.current.code && JSON.stringify(config) === JSON.stringify(baseline.current.config);
    // An editor that has not caught up with the restored draft yet must not
    // save its emptiness over it; emptying is what Discard draft is for.
    if (same || (!live.trim() && baseline.current.code.trim())) return undefined;
    unsaved.current = true;
    const timer = setTimeout(() => { saveDraft({ code: live, config }); }, DRAFT_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [live, config, graphId, restored, saveDraft]);

  // Leaving the panel (or the workflow) flushes what the debounce still owes.
  useEffect(() => () => {
    if (!unsaved.current || !graphId) return;
    writeLocalDraft(graphId, pending.current);
    api.saveEvaluatorDraft(graphId, pending.current).catch(() => {});
  }, [graphId]);

  const discardDraft = async () => {
    setCode(''); setLive(''); setConfig({}); setIface(null); setReport(null); setMetric('');
    baseline.current = { code: '', config: {} }; unsaved.current = false;
    clearLocalDraft(graphId);
    setDraftSavedAt(null); setDraftState('idle');
    try { await api.saveEvaluatorDraft(graphId, { code: '', config: {} }); }
    catch { setDraftState('local'); }
  };

  // ---- what to evaluate: a finished batch or run ----
  useEffect(() => {
    let active = true;
    setTargets([]); setSelection('');
    if (!graphId) return undefined;
    Promise.all([api.listBatches(graphId), api.listRuns(graphId)]).then(([batches, runs]) => {
      if (!active) return;
      const done = (r) => !['running', 'pending', 'queued', 'cancelling'].includes(r.status);
      const list = [
        ...rows(batches, 'batches').filter(done).map((r) => ({ id: `batch:${r.batch_id}`, label: `Batch ${r.batch_id} · ${r.total ?? '?'} runs · ${r.status}` })),
        ...rows(runs, 'runs').filter(done).map((r) => ({ id: `run:${r.run_id}`, label: `Run ${r.run_id} · ${r.status}` })),
      ];
      setTargets(list); setSelection(list[0]?.id || '');
    }).catch((e) => { if (active) setError(message(e)); });
    return () => { active = false; };
  }, [graphId, reload]);

  const targetBody = () => {
    const [kind, id] = selection.split(':');
    return { [kind === 'batch' ? 'batch_id' : 'run_id']: id };
  };

  const check = async () => {
    setChecking(true); setError(''); setIface(null);
    try {
      const result = await api.inspectEvaluatorCode(code);
      setIface(result);
      // Defaults belong in the form the moment they are known.
      if (!result?.error) setConfig((current) => {
        const next = { ...current };
        (result?.params || []).forEach((p) => { if (next[p.name] === undefined && p.default !== undefined && p.default !== null) next[p.name] = p.default; });
        return next;
      });
    } catch (e) { setIface({ error: message(e), params: [] }); }
    finally { setChecking(false); }
  };

  const evaluate = async (persist) => {
    setBusy(persist ? 'run' : 'preview'); setError(''); setReport(null);
    try {
      const body = { code, config, ...(metric ? { metric } : {}), ...targetBody() };
      const result = persist ? await api.runGraphEvaluator(graphId, body) : await api.previewGraphEvaluator(graphId, body);
      setReport(result); setReportLabel(name || (persist ? 'run' : 'preview'));
      const names = metricNames(result);
      if (names.length && !names.includes(metric)) setMetric(names[0]);
    } catch (e) { setError(message(e)); }
    finally { setBusy(''); }
  };

  const runSaved = async (evaluator) => {
    setBusy(`saved:${evaluator.name}`); setError(''); setReport(null);
    try {
      const result = await api.runGraphEvaluator(graphId, { name: evaluator.name, ...targetBody() });
      setReport(result); setReportLabel(evaluator.name);
    } catch (e) { setError(message(e)); }
    finally { setBusy(''); }
  };

  const replace = async (list) => {
    setError('');
    try {
      const answer = await api.saveGraphEvaluators(graphId, list);
      const next = rows(answer, 'evaluators');
      setSaved(next.length ? next : list);
      onEvaluatorsChanged?.(next.length ? next : list);
    } catch (e) { setError(message(e)); }
  };

  const saveEvaluator = async () => {
    const entry = {
      name: name.trim(), code, config, metric: metric || '', direction, timing,
      timeout: Number(timeout) || 120, labels: labels ? { resource_id: labels, loader: 'auto' } : null, enabled: true,
    };
    await replace([...saved.filter((e) => e.name !== entry.name), entry]);
  };

  const remove = async (evaluator) => {
    setError('');
    try { await api.deleteGraphEvaluator(graphId, evaluator.name); }
    catch (e) { setError(message(e)); return; }
    const next = saved.filter((e) => e.name !== evaluator.name);
    setSaved(next); onEvaluatorsChanged?.(next);
  };

  const edit = (evaluator) => {
    setCode(evaluator.code || ''); setConfig(evaluator.config || {}); setIface(null); setReport(null);
    setName(evaluator.name || ''); setMetric(evaluator.metric || ''); setTiming(evaluator.timing || 'manual');
    setTimeoutSeconds(evaluator.timeout ?? 120); setDirection(evaluator.direction || 'maximize');
    setLabels(evaluator.labels?.resource_id || '');
  };

  const metrics = useMemo(() => metricNames(report), [report]);
  const codeChanged = live !== code;
  const ready = !!graphId && !!code.trim() && !!selection && !codeChanged && !busy;

  if (!graphId) return <p className="muted">Open a workflow to write an evaluator.</p>;

  return <div className="evaluate-panel">
    <section>
      <h4>1 · Evaluation code</h4>
      <PythonDatasetEditor kind="Evaluator" code={code} onChange={setCode} onDraftChange={setLive}
        disabled={!!busy} example={EVALUATOR_EXAMPLE} examples={EXAMPLES} />
      <p className="muted small" role="status" data-testid="draft-status">
        {draftState === 'saving' ? 'Saving draft…'
          : draftState === 'local' ? 'Draft kept in this browser only: the server copy could not be written.'
          : draftSavedAt ? `Draft saved ${new Date(draftSavedAt).toLocaleString()}.`
          : 'Nothing pasted yet. Whatever you paste is kept as a draft, so you can leave to copy a path and come back.'}
        {' '}
        {(draftSavedAt || live.trim()) && <button type="button" className="link small" onClick={discardDraft}>Discard draft</button>}
      </p>
    </section>

    <section>
      <h4>2 · Parameters from the code</h4>
      <div className="input-actions">
        <button type="button" disabled={!code.trim() || checking} onClick={check}>{checking ? 'Checking…' : 'Check code'}</button>
        {codeChanged && <span className="muted small">Unconfirmed edits above. Confirm code, then check it again.</span>}
      </div>
      <EvaluatorParams iface={iface} values={config} onChange={setConfig} disabled={!!busy} />
    </section>

    <section>
      <h4>3 · Results to evaluate</h4>
      <div className="field"><label htmlFor="evaluate-target">Saved batch or run</label>
        <select id="evaluate-target" value={selection} onChange={(e) => { setSelection(e.target.value); setReport(null); }}>
          <option value="">Select a completed run or batch</option>
          {targets.map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
        </select></div>
      <div className="input-actions">
        <button type="button" disabled={!!busy} onClick={() => setReload((n) => n + 1)}>Refresh saved results</button>
        <button type="button" disabled={!ready} onClick={() => evaluate(false)}>{busy === 'preview' ? 'Evaluating…' : 'Preview'}</button>
        <button type="button" className="primary" disabled={!ready} onClick={() => evaluate(true)}>{busy === 'run' ? 'Running…' : 'Run and save report'}</button>
      </div>
      <p className="muted small">Preview keeps nothing. Run stores the report on that batch or run. The workflow is not rerun either way.</p>
      {error && <pre role="alert" className="json-view run-error">{String(error)}</pre>}
    </section>

    {report && <section>
      <h4>Report</h4>
      {metrics.length > 0 && <div className="field"><label htmlFor="evaluate-metric">Metric</label>
        <select id="evaluate-metric" value={metric} onChange={(e) => setMetric(e.target.value)}>
          {!metrics.includes(metric) && <option value="">Choose the metric Evolve optimizes</option>}
          {metrics.map((m) => <option key={m} value={m}>{m}</option>)}
        </select></div>}
      <EvaluatorReports reports={{ [reportLabel]: reportOf(report) }} />
    </section>}

    <section>
      <h4>4 · Keep it on this workflow</h4>
      <div className="field"><label htmlFor="evaluate-name">Name</label>
        <input id="evaluate-name" value={name} onChange={(e) => setName(e.target.value)} /></div>
      <div className="field"><label htmlFor="evaluate-timing">Run evaluation</label>
        <select id="evaluate-timing" value={timing} onChange={(e) => setTiming(e.target.value)}>
          <option value="manual">Only when asked</option>
          <option value="run">After each workflow run</option>
          <option value="batch">After the entire batch</option>
        </select></div>
      <div className="field"><label htmlFor="evaluate-timeout">Time limit (seconds)</label>
        <NumberInput id="evaluate-timeout" type="number" min={10} max={3600} step={1} value={timeout} onChange={(e) => setTimeoutSeconds(Number(e.target.value))} /></div>
      <div className="field"><label htmlFor="evaluate-direction">Optimize direction</label>
        <select id="evaluate-direction" value={direction} onChange={(e) => setDirection(e.target.value)}>
          <option value="maximize">Maximize</option><option value="minimize">Minimize</option>
        </select></div>
      <details><summary>Separate labels (optional)</summary>
        <p className="muted small">Labels are passed as config.label_records (or a label_records parameter). Code decides how to match them.</p>
        <div className="field"><label htmlFor="evaluate-labels">Label resource</label>
          <select id="evaluate-labels" value={labels} onChange={(e) => setLabels(e.target.value)}>
            <option value="">None</option>{resources.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
          </select></div></details>
      <div className="input-actions">
        <button type="button" className="primary" disabled={!name.trim() || !code.trim() || codeChanged} onClick={saveEvaluator}>Save as evaluator</button>
      </div>
      <p className="muted small">Saved evaluators are what Evolve chooses its objective from.</p>
    </section>

    <section>
      <h4>Saved evaluators</h4>
      {saved.length === 0 ? <p className="muted small" data-testid="no-evaluators">None yet. Write code above and save it.</p>
        : <table className="eval-table"><thead><tr><th>Name</th><th>Metric</th><th>Timing</th><th>On</th><th /></tr></thead><tbody>
          {saved.map((e) => <tr key={e.name}>
            <td>{e.name}</td><td>{e.metric || '—'}</td><td>{e.timing || 'manual'}</td>
            <td><input type="checkbox" aria-label={`Enable ${e.name}`} checked={e.enabled !== false}
              onChange={(event) => replace(saved.map((x) => (x.name === e.name ? { ...x, enabled: event.target.checked } : x)))} /></td>
            <td className="input-actions">
              <button type="button" onClick={() => edit(e)}>Edit</button>
              <button type="button" disabled={!selection || !!busy} onClick={() => runSaved(e)}>{busy === `saved:${e.name}` ? 'Running…' : 'Run'}</button>
              <button type="button" onClick={() => remove(e)}>Delete</button>
            </td></tr>)}
        </tbody></table>}
    </section>
  </div>;
}
