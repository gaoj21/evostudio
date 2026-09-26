import React from 'react';
import EvaluatorReports from './EvaluatorReports.jsx';

/**
 * How a batch did: the reports evaluations of it left.
 *
 * An evaluation is code brought to saved results in Evaluation & Evolve
 * (New run → Evaluation); one run on this batch keeps its report here too,
 * without rerunning the workflow. Reports left by earlier code stay readable.
 */
export default function EvaluationTab({ batch }) {
  const shown = batch?.evaluations || {};
  return (
    <div className="drawer-body eval-tab">
      {Object.keys(shown).length === 0 && (
        <p className="muted small" data-testid="no-evaluation">
          No evaluation of this batch yet. In Evaluation &amp; Evolve, start a New run → Evaluation with your code and
          choose this batch: the report is kept here.
        </p>
      )}
      <EvaluatorReports reports={shown} />
    </div>
  );
}
