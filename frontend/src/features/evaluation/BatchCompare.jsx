import React from 'react';
import JsonView from '../../components/JsonView.jsx';

// "+0.12" / "−1.0" — the sign is the whole message, so it is never dropped.
export function signed(value, digits = 2) {
  if (value == null) return '—';
  const rounded = Number(value).toFixed(digits);
  if (Number(rounded) === 0) return '0.00';
  return Number(rounded) > 0 ? `+${rounded}` : rounded.replace('-', '−');
}

export function deltaClass(value) {
  if (value == null) return 'delta-none';
  if (value > 0) return 'delta-up';
  if (value < 0) return 'delta-down';
  return 'delta-flat';
}

function score(value) {
  return value == null ? '—' : Number(value).toFixed(2);
}

/**
 * Two evaluations of the same workflow, side by side.
 *
 * The headline is the mean, but the reason to look is underneath it: a mean
 * that improved can still hide records that broke, so the per-record list
 * leads with the regressions.
 */
export default function BatchCompare({ comparison, onBack }) {
  const c = comparison;
  const moved = (c.changes || []).filter((x) => x.delta !== 0);

  return (
    <div className="compare-view">
      <button type="button" className="compare-back" onClick={onBack}>
        ← Back to batches
      </button>

      {(c.notes || []).map((note, i) => (
        <div key={i} className="chat-note">{note}</div>
      ))}

      <div className="compare-headline">
        <div className="compare-means">
          <span className="compare-mean">{score(c.mean_before)}</span>
          <span className="compare-arrow">→</span>
          <span className="compare-mean">{score(c.mean_after)}</span>
          <span className={`compare-delta ${deltaClass(c.delta)}`}>{signed(c.delta)}</span>
        </div>
        <div className="muted small">
          {c.candidate?.metric || 'no metric'} over {c.matched} shared record
          {c.matched === 1 ? '' : 's'}
        </div>
      </div>

      <div className="compare-counts">
        <span className="delta-up">{c.improved} better</span>
        <span className="delta-down">{c.regressed} worse</span>
        <span className="muted">{c.unchanged} unchanged</span>
        {c.unscored > 0 && <span className="muted">{c.unscored} unscored</span>}
      </div>

      {c.matched === 0 ? null : moved.length === 0 ? (
        <p className="muted small">Every shared record scored exactly the same.</p>
      ) : (
        <div className="compare-list">
          {moved.map((change, i) => (
            <div className="compare-row" key={`${change.label}-${i}`}>
              <span className={`compare-delta ${deltaClass(change.delta)}`}>
                {signed(change.delta)}
              </span>
              <span className="compare-row-main">
                <span className="compare-row-label">{change.label}</span>
                <span className="muted small">
                  {score(change.score_before)} → {score(change.score_after)}
                  {change.status_after !== 'success' && ` · ${change.status_after}`}
                </span>
              </span>
              {change.output_after && (
                <JsonView value={change.output_after} className="compare-output" />
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
