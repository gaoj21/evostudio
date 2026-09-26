import React, { useCallback, useEffect, useRef, useState } from 'react';
import NumberInput from '../../components/NumberInput.jsx';
import { api } from '../../api.js';
import PythonDatasetEditor from '../data/PythonDatasetEditor.jsx';
import EvaluatorParams from './EvaluatorParams.jsx';
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

/**
 * The code an evaluation runs: pasted or uploaded here, its parameters read
 * from the code itself. It is kept as a draft for this workflow — leave to
 * copy a run's path and come back to it — and travels with the evaluation
 * that runs it; nothing is saved on the workflow.
 *
 * `onChange({ code, config, timeout, labels, ready })`: `ready` once the code
 * is confirmed and its parameters read without error.
 */
export default function EvaluationCode({ graphId, onChange, disabled = false }) {
  const [code, setCode] = useState('');            // confirmed code: what runs
  const [live, setLive] = useState('');            // what is in the editor, unconfirmed included
  const [config, setConfig] = useState({});
  const [iface, setIface] = useState(null);
  const [checking, setChecking] = useState(false);
  const [timeout, setTimeoutSeconds] = useState(120);
  const [labels, setLabels] = useState('');
  const [resources, setResources] = useState([]);
  const [restored, setRestored] = useState(false);
  const [draftState, setDraftState] = useState('idle');
  const [draftSavedAt, setDraftSavedAt] = useState(null);
  const baseline = useRef({ code: '', config: {} });
  const pending = useRef({ code: '', config: {} });
  const unsaved = useRef(false);

  useEffect(() => { api.listDataResources?.().then((r) => setResources(rows(r, 'resources'))).catch(() => {}); }, []);

  // ---- the draft: server copy wins, browser copy is the safety net ----
  useEffect(() => {
    let active = true;
    setRestored(false); setCode(''); setLive(''); setConfig({}); setIface(null);
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

  // Leaving (the form, or the workflow) flushes what the debounce still owes.
  useEffect(() => () => {
    if (!unsaved.current || !graphId) return;
    writeLocalDraft(graphId, pending.current);
    api.saveEvaluatorDraft(graphId, pending.current).catch(() => {});
  }, [graphId]);

  const discardDraft = async () => {
    setCode(''); setLive(''); setConfig({}); setIface(null);
    baseline.current = { code: '', config: {} }; unsaved.current = false;
    clearLocalDraft(graphId);
    setDraftSavedAt(null); setDraftState('idle');
    try { await api.saveEvaluatorDraft(graphId, { code: '', config: {} }); }
    catch { setDraftState('local'); }
  };

  // The parameters are read from the code as soon as there is code to read.
  const check = useCallback(async (source) => {
    setChecking(true); setIface(null);
    try {
      const result = await api.inspectEvaluatorCode(source);
      setIface(result);
      if (!result?.error) setConfig((current) => {
        const next = { ...current };
        (result?.params || []).forEach((p) => { if (next[p.name] === undefined && p.default !== undefined && p.default !== null) next[p.name] = p.default; });
        return next;
      });
    } catch (e) { setIface({ error: message(e), params: [] }); }
    finally { setChecking(false); }
  }, []);
  useEffect(() => { if (restored && code.trim()) check(code); }, [restored, code, check]);

  const codeChanged = live !== code;
  const ready = !!code.trim() && !codeChanged && !checking && !!iface && !iface.error;
  useEffect(() => {
    onChange?.({ code, config, timeout: Number(timeout) || 120,
      labels: labels ? { resource_id: labels, loader: 'auto' } : null, ready });
  }, [code, config, timeout, labels, ready, onChange]);

  return <div className="evaluation-code">
    <PythonDatasetEditor kind="Evaluator" code={code} onChange={setCode} onDraftChange={setLive}
      disabled={disabled} example={EVALUATOR_EXAMPLE} examples={EXAMPLES} />
    <p className="muted small" role="status" data-testid="draft-status">
      {draftState === 'saving' ? 'Saving draft…'
        : draftState === 'local' ? 'Draft kept in this browser only: the server copy could not be written.'
        : draftSavedAt ? `Draft saved ${new Date(draftSavedAt).toLocaleString()}.`
        : 'Paste or upload the evaluation code. What you paste is kept as a draft, so you can leave to copy a path and come back.'}
      {' '}
      {(draftSavedAt || live.trim()) && <button type="button" className="link small" onClick={discardDraft}>Discard draft</button>}
    </p>
    <div className="input-actions">
      <button type="button" disabled={!code.trim() || checking || disabled} onClick={() => check(code)}>{checking ? 'Reading parameters…' : 'Read parameters again'}</button>
      {codeChanged && <span className="muted small">Unconfirmed edits above: confirm the code to evaluate it.</span>}
    </div>
    <EvaluatorParams iface={iface} values={config} onChange={setConfig} disabled={disabled} />
    <details className="evaluate-settings">
      <summary>Time limit and labels</summary>
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
