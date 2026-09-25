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
// A workflow has one evaluation: this code. It is kept under this name, which
// the page never shows; a workflow saved with a differently named one keeps it.
export const WORKFLOW_EVALUATION = 'evaluation';
export const workflowEvaluation = (list) =>
  (list || []).find((e) => e.name === WORKFLOW_EVALUATION) || (list || [])[0] || null;
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
 * Evaluation as code: paste or upload it here, read its parameters from the
 * code itself, and try it on a saved batch or run. The workflow has one
 * evaluation — this code — kept as it is confirmed; Evolve optimizes what it
 * scores. Nothing about evaluation lives on the canvas.
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
  const [kept, setKept] = useState(null);          // the workflow's evaluation, as last kept
  const [others, setOthers] = useState([]);        // anything else saved on the workflow, left alone
  const [resources, setResources] = useState([]);
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

  // ---- the draft: server copy wins, browser copy is the safety net ----
  useEffect(() => {
    let active = true;
    setRestored(false); setCode(''); setConfig({}); setIface(null); setReport(null); setMetric('');
    baseline.current = { code: '', config: {} }; unsaved.current = false;
    if (!graphId) return undefined;
    setKept(null); setOthers([]);
    (async () => {
      let server = null;
      let list = [];
      try { server = await api.evaluatorDraft(graphId); } catch { server = null; }
      try { list = rows(await api.graphEvaluators(graphId), 'evaluators'); } catch (e) { setError(message(e)); }
      if (!active) return;
      const current = workflowEvaluation(list);
      setKept(current); setOthers(list.filter((e) => e !== current));
      if (current) {
        setMetric(current.metric || ''); setDirection(current.direction || 'maximize');
        setTimeoutSeconds(current.timeout ?? 120); setLabels(current.labels?.resource_id || '');
      }
      // Pasted-but-unconfirmed code is newer than what was kept.
      const draft = newerDraft(server, readLocalDraft(graphId)) || {};
      const start = draft.code ? draft : { code: current?.code || '', config: current?.config || {} };
      setCode(start.code || ''); setConfig(start.config || {}); setDraftSavedAt(draft.saved_at || null);
      setDraftState(draft.code ? 'saved' : 'idle');
      baseline.current = { code: start.code || '', config: start.config || {} };
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
      setReport(result); setReportLabel(persist ? 'run' : 'preview');
      const names = metricNames(result);
      if (names.length && !names.includes(metric)) setMetric(names[0]);
    } catch (e) { setError(message(e)); }
    finally { setBusy(''); }
  };

  // ---- keep it: the confirmed code and its settings are the workflow's evaluation ----
  useEffect(() => {
    if (!graphId || !restored || !code.trim()) return undefined;
    const entry = {
      name: kept?.name || WORKFLOW_EVALUATION, code, config, metric: metric || '', direction,
      timing: 'manual', timeout: Number(timeout) || 120,
      labels: labels ? { resource_id: labels, loader: 'auto' } : null, enabled: true,
    };
    const same = kept && ['code', 'metric', 'direction', 'timeout'].every((k) => kept[k] === entry[k])
      && JSON.stringify(kept.config || {}) === JSON.stringify(entry.config)
      && (kept.labels?.resource_id || '') === (labels || '');
    if (same) return undefined;
    const timer = setTimeout(async () => {
      try {
        const answer = rows(await api.saveGraphEvaluators(graphId, [...others, entry]), 'evaluators');
        const current = answer.find((e) => e.name === entry.name) || entry;
        setKept(current);
        onEvaluatorsChanged?.(answer.length ? answer : [...others, entry]);
      } catch (e) { setError(message(e)); }
    }, DRAFT_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [graphId, restored, code, config, metric, direction, timeout, labels, kept, others, onEvaluatorsChanged]);

  const metrics = useMemo(() => metricNames(report), [report]);
  const codeChanged = live !== code;
  const ready = !!graphId && !!code.trim() && !!selection && !codeChanged && !busy;

  if (!graphId) return <p className="muted">Open a workflow to evaluate it.</p>;

  return <div className="evaluate-panel">
    <section>
      <h4>1 · Evaluation code</h4>
      <PythonDatasetEditor kind="Evaluator" code={code} onChange={setCode} onDraftChange={setLive}
        disabled={!!busy} example={EVALUATOR_EXAMPLE} examples={EXAMPLES} />
      <p className="muted small" role="status" data-testid="draft-status">
        {draftState === 'saving' ? 'Saving draft…'
          : draftState === 'local' ? 'Draft kept in this browser only: the server copy could not be written.'
          : kept?.code && kept.code === code && !codeChanged ? 'This is the workflow\'s evaluation code. Evolve optimizes what it scores.'
          : draftSavedAt ? `Draft saved ${new Date(draftSavedAt).toLocaleString()}. Confirm the code to make it the workflow's evaluation.`
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
          {!metrics.includes(metric) && <option value="">{metric ? `${metric} (not in this report)` : 'Choose the metric Evolve optimizes'}</option>}
          {metrics.map((m) => <option key={m} value={m}>{m}</option>)}
        </select></div>}
      <EvaluatorReports reports={{ [reportLabel]: reportOf(report) }} />
    </section>}

    <details className="evaluate-settings">
      <summary>Settings</summary>
      {!metrics.length && <div className="field"><label htmlFor="evaluate-objective">Metric Evolve optimizes</label>
        <input id="evaluate-objective" value={metric} placeholder="Preview once to choose it from the report"
          onChange={(e) => setMetric(e.target.value)} /></div>}
      <div className="field"><label htmlFor="evaluate-direction">Optimize direction</label>
        <select id="evaluate-direction" value={direction} onChange={(e) => setDirection(e.target.value)}>
          <option value="maximize">Maximize</option><option value="minimize">Minimize</option>
        </select></div>
      <div className="field"><label htmlFor="evaluate-timeout">Time limit (seconds)</label>
        <NumberInput id="evaluate-timeout" type="number" min={10} max={3600} step={1} value={timeout} onChange={(e) => setTimeoutSeconds(Number(e.target.value))} /></div>
      <p className="muted small">Labels are passed as config.label_records (or a label_records parameter). Code decides how to match them.</p>
      <div className="field"><label htmlFor="evaluate-labels">Label resource (optional)</label>
        <select id="evaluate-labels" value={labels} onChange={(e) => setLabels(e.target.value)}>
          <option value="">None</option>{resources.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
        </select></div>
    </details>
  </div>;
}
