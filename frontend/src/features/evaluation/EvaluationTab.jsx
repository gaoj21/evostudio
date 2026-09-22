import React, { useState } from 'react';
import { api } from '../../api.js';
import EvaluatorReports from './EvaluatorReports.jsx';

/**
 * How a batch did, by the workflow's own Evaluator nodes.
 *
 * Evaluation is the user's code on the canvas. Evaluators timed "after the
 * entire batch" already reported when the batch finished; any evaluator can
 * also be run on these saved results here, without rerunning the workflow,
 * and the report is kept with the batch.
 */
export default function EvaluationTab({ batch }) {
  const [reports, setReports] = useState(batch?.evaluations || {});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const settled = batch && !['running', 'cancelling'].includes(batch.status);
  const shown = { ...(batch?.evaluations || {}), ...reports };

  async function evaluate() {
    setBusy(true);
    setError(null);
    try {
      const value = await api.evaluateCanvasResults(batch.graph_id, { batch_id: batch.batch_id });
      setReports(value.evaluations || {});
    } catch (err) {
      setError(err?.body?.detail || err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="drawer-body eval-tab">
      <div className="eval-actions">
        {batch?.graph_id && (
          <button type="button" className="primary" disabled={!settled || busy} onClick={evaluate}>
            {busy ? 'Evaluating…' : 'Evaluate with the canvas evaluators'}
          </button>
        )}
        <span className="muted small">Runs the Evaluator nodes on these saved results. The workflow is not rerun.</span>
        {!settled && <span className="muted small">Available when the batch has finished.</span>}
      </div>
      {error && <pre role="alert" className="json-view run-error">{String(error)}</pre>}
      {Object.keys(shown).length === 0 && !error && (
        <p className="muted small" data-testid="no-evaluation">
          No evaluation yet. Add an Evaluator node to the canvas and drop your Python code into it, then evaluate here.
        </p>
      )}
      <EvaluatorReports reports={shown} />
    </div>
  );
}
