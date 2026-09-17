import React from 'react';
import JsonView from '../../components/JsonView.jsx';
export default function EvaluatorReports({reports}) {
 if (!reports || !Object.keys(reports).length) return null;
 return <section aria-label="Evaluator reports"><h4>Evaluator reports</h4>{Object.entries(reports).map(([name,r])=><details key={name}><summary>{name} · {r.status}{r.coverage ? ` · ${r.coverage.scored}/${r.coverage.total} ${r.coverage.unit || "records"} scored`:''}</summary>{r.error?<p role="alert">{r.error}</p>:<JsonView value={r}/>}</details>)}</section>;
}
