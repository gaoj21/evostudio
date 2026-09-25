import React, { useEffect, useState } from 'react';
import { api } from '../../api.js';
import EvaluatorReports from './EvaluatorReports.jsx';
import { workflowEvaluation } from './EvaluatePanel.jsx';

/**
 * How a batch did, by the workflow's evaluation code.
 *
 * Evaluation is the user's code, written in Evaluate & Evolve and kept on the
 * workflow. It runs here on these saved results, without rerunning the
 * workflow, and the report is kept with the batch. Reports left by earlier
 * code stay readable.
 */
export default function EvaluationTab({ batch }) {
  const [reports, setReports] = useState({});
  const [evaluation, setEvaluation] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const settled = batch && !['running', 'cancelling'].includes(batch.status);
  const shown = { ...(batch?.evaluations || {}), ...reports };

  useEffect(() => {
    let active = true;
    setReports({}); setEvaluation(null);
    if (!batch?.graph_id) return undefined;
    api.graphEvaluators(batch.graph_id).then((r) => {
      if (!active) return;
      setEvaluation(workflowEvaluation((Array.isArray(r) ? r : r.evaluators || []).filter((e) => e.enabled !== false)));
    }).catch(() => {});
    return () => { active = false; };
  }, [batch?.graph_id]);

  async function evaluate() {
    setBusy(true);
    setError(null);
    try {
      const value = await api.runGraphEvaluator(batch.graph_id, { name: evaluation.name, batch_id: batch.batch_id });
      // The run answers with every report it produced, by evaluator.
      setReports((current) => ({ ...current, ...(value?.evaluations || { [evaluation.name]: value?.report || value }) }));
    } catch (err) {
      setError(err?.body?.detail || err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="drawer-body eval-tab">
      {batch?.graph_id && evaluation && (
        <div className="eval-actions">
          <button type="button" className="primary" disabled={!settled || busy} onClick={evaluate}>
            {busy ? 'Evaluating…' : 'Evaluate these results'}
          </button>
          <span className="muted small">Runs the workflow&apos;s evaluation code on these saved results. The workflow is not rerun.</span>
          {!settled && <span className="muted small">Available when the batch has finished.</span>}
        </div>
      )}
      {error && <pre role="alert" className="json-view run-error">{String(error)}</pre>}
      {Object.keys(shown).length === 0 && !error && (
        <p className="muted small" data-testid="no-evaluation">
          No evaluation yet. Write the workflow&apos;s evaluation code in Evaluate &amp; Evolve, then evaluate these results here.
        </p>
      )}
      <EvaluatorReports reports={shown} />
    </div>
  );
}
