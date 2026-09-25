import React, { useEffect, useState } from 'react';
import { api } from '../../api.js';
import EvaluatorReports from './EvaluatorReports.jsx';

/**
 * How a batch did, by the workflow's own evaluators.
 *
 * Evaluation is the user's code, written in Evaluate & Evolve and kept on the
 * workflow. Evaluators timed "after the entire batch" already reported when
 * the batch finished; any saved evaluator can also be run on these saved
 * results here, without rerunning the workflow, and the report is kept with
 * the batch. Reports a removed (or migrated-away) evaluator left behind stay
 * readable.
 */
export default function EvaluationTab({ batch }) {
  const [reports, setReports] = useState({});
  const [evaluators, setEvaluators] = useState([]);
  const [chosen, setChosen] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const settled = batch && !['running', 'cancelling'].includes(batch.status);
  const shown = { ...(batch?.evaluations || {}), ...reports };

  useEffect(() => {
    let active = true;
    setReports({}); setEvaluators([]); setChosen('');
    if (!batch?.graph_id) return undefined;
    api.graphEvaluators(batch.graph_id).then((r) => {
      if (!active) return;
      const list = (Array.isArray(r) ? r : r.evaluators || []).filter((e) => e.enabled !== false);
      setEvaluators(list); setChosen(list[0]?.name || '');
    }).catch(() => {});
    return () => { active = false; };
  }, [batch?.graph_id]);

  async function evaluate() {
    setBusy(true);
    setError(null);
    try {
      const value = await api.runGraphEvaluator(batch.graph_id, { name: chosen, batch_id: batch.batch_id });
      setReports((current) => ({ ...current, [chosen]: value?.report || value }));
    } catch (err) {
      setError(err?.body?.detail || err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="drawer-body eval-tab">
      {batch?.graph_id && evaluators.length > 0 && (
        <div className="eval-actions">
          <select aria-label="Evaluator" value={chosen} onChange={(e) => setChosen(e.target.value)}>
            {evaluators.map((e) => <option key={e.name} value={e.name}>{e.name}{e.metric ? ` · ${e.metric}` : ''}</option>)}
          </select>
          <button type="button" className="primary" disabled={!settled || busy || !chosen} onClick={evaluate}>
            {busy ? 'Evaluating…' : 'Run this evaluator on these results'}
          </button>
          <span className="muted small">Runs your saved code on these saved results. The workflow is not rerun.</span>
          {!settled && <span className="muted small">Available when the batch has finished.</span>}
        </div>
      )}
      {error && <pre role="alert" className="json-view run-error">{String(error)}</pre>}
      {Object.keys(shown).length === 0 && !error && (
        <p className="muted small" data-testid="no-evaluation">
          No evaluation yet. Write your Python evaluation code in Evaluate &amp; Evolve, save it on this workflow, then run it here.
        </p>
      )}
      <EvaluatorReports reports={shown} />
    </div>
  );
}
