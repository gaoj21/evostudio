import React, { useCallback, useEffect, useState } from 'react';
import { api } from '../../api.js';
import EvaluatorReports from './EvaluatorReports.jsx';
import JsonView from '../../components/JsonView.jsx';

/**
 * What a batch got right, computed after the fact.
 *
 * Nothing to decide before the run: press Evaluate on any batch. Credit-risk
 * batches carry their own truth and get the report that matters — caught or
 * missed, how early, false alarms, weekly bands. Anything else picks a metric
 * and the field holding the expected answer, here, once the results exist.
 */
export default function EvaluationTab({ batch }) {
  const [canvasReports, setCanvasReports] = useState(batch?.evaluations || {});
  const [report, setReport] = useState(batch?.evaluation || null);
  const [creditRisk, setCreditRisk] = useState(null);
  const [metrics, setMetrics] = useState([]);
  const [metric, setMetric] = useState('exact_match');
  const [labelKey, setLabelKey] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const batchId = batch?.batch_id;

  useEffect(() => {
    if (!batchId) return;
    api.getBatchEvaluation(batchId)
      .then((res) => { setReport(res.evaluation || null); setCreditRisk(!!res.credit_risk); })
      .catch(() => setCreditRisk(false));
  }, [batchId]);

  useEffect(() => {
    if (creditRisk === false) api.listMetrics().then((r) => setMetrics(r.metrics || [])).catch(() => {});
  }, [creditRisk]);

  const evaluate = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const body = creditRisk ? undefined : { metric, label_key: labelKey };
      setReport(await api.evaluateBatch(batchId, body));
    } catch (err) {
      setError(err?.body?.detail || err.message);
    } finally {
      setBusy(false);
    }
  }, [batchId, creditRisk, metric, labelKey]);

  const settled = batch && !['running', 'cancelling'].includes(batch.status);
  const stale = report && batch && report.batch_status !== batch.status;

  return (
    <div className="drawer-body eval-tab">
      <EvaluatorReports reports={{...batch?.evaluations,...canvasReports}} />
      {batch?.graph_id && <button disabled={!settled || busy} onClick={async()=>{setBusy(true);setError(null);try{const value=await api.evaluateCanvasResults(batch.graph_id,{batch_id:batchId});setCanvasReports(value.evaluations);}catch(e){setError(e.body?.detail || e.message);}finally{setBusy(false);}}}>Evaluate with canvas evaluators (no workflow rerun)</button>}
      <div className="eval-actions">
        {creditRisk === false && (
          <>
            <select aria-label="Metric" value={metric} onChange={(e) => setMetric(e.target.value)}>
              {metrics.map((m) => <option key={m.name} value={m.name}>{m.name}{m.custom ? ' (custom)' : ''}</option>)}
              {metrics.length === 0 && <option value="exact_match">exact_match</option>}
            </select>
            <input
              aria-label="Field holding the expected answer"
              placeholder="field holding the expected answer"
              value={labelKey}
              onChange={(e) => setLabelKey(e.target.value)}
            />
          </>
        )}
        <button type="button" className="primary" onClick={evaluate} disabled={busy || creditRisk === null}>
          {busy ? 'Evaluating…' : report ? 'Evaluate again' : 'Evaluate'}
        </button>
        {creditRisk && (
          <span className="muted small">
            The records carry their own truth (bankruptcy date or none); nothing to configure.
          </span>
        )}
        {!settled && <span className="muted small">Still running: the report covers what has finished so far.</span>}
        {stale && settled && <span className="muted small">The batch changed since this report; evaluate again.</span>}
      </div>
      {error && <div className="muted small batch-error">{String(error)}</div>}
      {!report && !error && (
        <p className="muted small">
          {creditRisk
            ? 'Press Evaluate: which companies were caught, how many days ahead, which negatives raised alarms, and how each week\'s level compares with the countdown.'
            : 'Press Evaluate to score every finished record with a metric.'}
        </p>
      )}
      {report?.kind === 'credit_risk' && <CreditRiskReport report={report} />}
      {report?.kind === 'metric' && <MetricReport report={report} />}
    </div>
  );
}

const pct = (x) => (x == null ? '—' : `${Math.round(x * 100)}%`);

function Tally({ title, t }) {
  return (
    <div className="eval-card">
      <div className="muted small">{title}</div>
      <div className="eval-card-row">
        <span className="eval-stat"><b>{t.detected}/{t.positives}</b> caught</span>
        <span className="eval-stat"><b>{t.false_alarms}/{t.negatives}</b> false alarms</span>
        <span className="eval-stat"><b>{t.mean_lead_days ?? '—'}</b>{t.mean_lead_days != null && 'd'} lead</span>
        {t.weeks > 0 && <span className="eval-stat"><b>{pct(t.near_rate)}</b> within ±1 reviewed band</span>}
        {t.countdown_weeks > 0 && <span className="eval-stat" title="Positives' weekly level against the band the countdown to filing implies: an early-warning indicator, not a judged truth"><b>{pct(t.countdown_near_rate)}</b> within ±1 countdown</span>}
        {t.unverified > 0 && <span className="eval-stat muted"><b>{t.unverified}</b> unverified, not scored</span>}
      </div>
    </div>
  );
}

function CreditRiskReport({ report }) {
  const [open, setOpen] = useState(null);
  return (
    <>
      <div className="eval-cards">
        {report.flagged?.length > 0 && <Tally title={`Without flagged samples (${report.flagged.length} excluded)`} t={report.unflagged} />}
        <Tally title={report.flagged?.length > 0 ? 'All samples' : 'All samples'} t={report.all} />
      </div>
      {report.flagged?.length > 0 && (
        <div className="muted small">
          Flagged by the dataset: {report.flagged.map((f) => `${f.company} (${f.flags.join(', ')})`).join('; ')}.
          Their headlines are not about the company, so a miss there is the data's.
        </div>
      )}
      {report.no_ground_truth && (
        <div className="muted small batch-error">
          None of these companies has a verified outcome in the dataset this batch ran on, so nothing here can be
          called caught or a false alarm. The steps are still listed below.
        </div>
      )}
      {!report.no_ground_truth && report.unverified?.length > 0 && (
        <div className="muted small">
          {report.unverified.length} compan{report.unverified.length === 1 ? 'y' : 'ies'} without a verified outcome
          are listed but not scored: {report.unverified.slice(0, 6).map((u) => u.company).join(', ')}{report.unverified.length > 6 ? '…' : ''}.
          The dataset has no verified negatives yet, so false alarms cannot be counted on it.
        </div>
      )}
      {report.foresight?.length > 0 && (
        <div className="muted small batch-error">
          {report.foresight.length} step(s) name a date after their own — the model used what it knows, not the material:{' '}
          {report.foresight.map((f) => `${f.company} ${f.as_of} (${f.mentions.join(', ')})`).join('; ')}.
        </div>
      )}
      {report.failed_steps > 0 && (
        <div className="muted small">{report.failed_steps} step(s) did not finish and are not judged.</div>
      )}
      <table className="eval-table">
        <thead>
          <tr>
            <th>Company</th><th>Truth</th><th>Result</th><th>First high</th><th>Lead</th><th>Bands</th><th>Steps (level vs expected)</th>
          </tr>
        </thead>
        <tbody>
          {report.companies.map((c) => (
            <React.Fragment key={c.sample_id}>
              <tr className={c.flags?.length ? 'eval-flagged' : ''} onClick={() => setOpen(open === c.sample_id ? null : c.sample_id)}>
                <td><b>{c.company}</b>{c.flags?.length > 0 && <span className="muted small"> flagged</span>}</td>
                <td>{c.type === 'positive' ? `bankrupt ${c.event_date}` : c.type === 'negative' ? 'no event' : <span className="muted" title={c.unverified_reason || ''}>unverified</span>}</td>
                <td>
                  {c.type === 'positive'
                    ? (c.detected ? <span className="eval-good">caught</span> : <span className="eval-bad">missed</span>)
                    : c.type === 'negative'
                      ? (c.false_alarm ? <span className="eval-bad">false alarm</span> : c.false_alarm === false ? <span className="eval-good">clean</span> : <span className="muted">not scored</span>)
                      : <span className="muted">—</span>}
                </td>
                <td>{c.first_alert || '—'}</td>
                <td>{c.lead_days != null ? `${c.lead_days}d` : '—'}</td>
                <td>{c.weeks ? `${c.weeks_near}/${c.weeks} ±1 · ${c.weeks_exact} exact` : '—'}</td>
                <td className="eval-steps">
                  {c.steps.map((s) => (
                    <span
                      key={s.as_of}
                      className={`eval-step band-${s.risk_level || 'none'} match-${s.match || 'none'}`}
                      title={`${s.as_of}: ${s.action || s.status} · ${s.risk_level || '—'} ${s.score ?? ''} (expected ${s.expected})`}
                    >
                      {(s.risk_level || s.status || '?').slice(0, 4)}
                    </span>
                  ))}
                </td>
              </tr>
              {open === c.sample_id && (
                <tr className="eval-detail">
                  <td colSpan={7}>
                    {c.steps.map((s) => (
                      <div className="eval-detail-row" key={s.as_of}>
                        <span className="memory-at">{s.as_of}</span>
                        <span className={`eval-step band-${s.risk_level || 'none'} match-${s.match || 'none'}`}>{s.risk_level || s.status}</span>
                        <span className="muted small">expected {s.expected}</span>
                        <span>{s.action} · {s.score ?? '—'}</span>
                        <span className="muted small eval-rationale">{s.rationale || ''}</span>
                      </div>
                    ))}
                  </td>
                </tr>
              )}
            </React.Fragment>
          ))}
        </tbody>
      </table>
      <div className="muted small">
        Expected band from the countdown: ≤30 days critical, ≤90 high, ≤180 medium, no event low.
        "Caught" means a high or critical level before the event. Click a row for each step's rationale.
      </div>
    </>
  );
}

function MetricReport({ report }) {
  const s = report.summary || {};
  return (
    <>
      <div className="eval-cards">
        <div className="eval-card">
          <div className="muted small">{report.metric} · expected answer in <code>{report.label_key}</code></div>
          <div className="eval-card-row">
            <span className="eval-stat"><b>{s.mean ?? '—'}</b> mean</span>
            <span className="eval-stat"><b>{s.scored}/{s.total}</b> scored</span>
            <span className="eval-stat"><b>{s.perfect}</b> perfect</span>
            {s.mean != null && <span className="eval-stat"><b>{s.min}–{s.max}</b> range</span>}
          </div>
        </div>
      </div>
      <div className="batch-list">
        {report.items.map((it) => (
          <div className="batch-item" key={it.index}>
            <div className="batch-item-head">
              <span>#{it.index + 1}</span>
              <span className="muted small">
                {it.score == null ? it.status : `score ${it.score}`} · expected {JSON.stringify(it.label)}
              </span>
            </div>
            {it.detail && <JsonView value={it.detail} className="batch-output" startOpen={false} />}
          </div>
        ))}
      </div>
    </>
  );
}
