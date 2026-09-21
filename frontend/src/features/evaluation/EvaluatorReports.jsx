import React from 'react';
import JsonView from '../../components/JsonView.jsx';

// What each evaluator reported: its objective, what the code printed, and
// when it failed, the error with the line in the user's code.
export default function EvaluatorReports({reports}) {
  if (!reports || !Object.keys(reports).length) return null;
  return <section aria-label="Evaluator reports"><h4>Evaluator reports</h4>{Object.entries(reports).map(([name, r]) => {
    const {logs, config, ...report} = r;
    const objective = r.objective && r.metrics ? r.metrics[r.objective.metric] : undefined;
    return <details key={name}>
      <summary>{name} · {r.status}
        {r.objective && r.status === 'success' ? ` · ${r.objective.metric} ${objective ?? '—'}` : ''}
        {r.coverage ? ` · ${r.coverage.scored}/${r.coverage.total} ${r.coverage.unit || 'records'} scored` : ''}</summary>
      {r.objective?.chosen && <p className="muted small" data-testid="objective-note">No objective metric chosen yet; showing the first metric your code returned ({r.objective.metric}). Choose one in the evaluator settings before using it in Evolve.</p>}
      {r.error ? <pre role="alert" className="json-view run-error">{r.error}</pre> : <JsonView value={report}/>}
      {logs && <details><summary>Printed output</summary><pre className="json-view" data-testid="evaluator-logs">{logs}</pre></details>}
    </details>;
  })}</section>;
}
