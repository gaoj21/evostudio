import React, { useEffect, useRef, useState } from 'react';
import { api } from '../api.js';

export default function ReviewPanel({ open, onClose }) {
  const [reviews, setReviews] = useState([]);
  const [notes, setNotes] = useState({});
  const [error, setError] = useState(null);
  const pollRef = useRef(null);

  const refresh = async () => {
    try {
      setReviews(await api.listReviews());
    } catch {
      /* transient poll error */
    }
  };

  useEffect(() => {
    if (!open) return undefined;
    refresh();
    pollRef.current = setInterval(refresh, 4000);
    return () => clearInterval(pollRef.current);
  }, [open]);

  if (!open) return null;

  const resolve = async (reviewId, decision) => {
    setError(null);
    try {
      await api.resolveReview(reviewId, decision, notes[reviewId] || '');
      await refresh();
    } catch (err) {
      setError(err?.body?.detail || err.message);
    }
  };

  const pending = reviews.filter((r) => r.status === 'pending');
  const resolved = reviews.filter((r) => r.status !== 'pending');

  const card = (r) => (
    <div className="batch-item" key={r.review_id}>
      <div className="batch-item-head">
        <span>{r.company || '(unknown company)'}</span>
        <span className={`node-status status-${r.status === 'pending' ? 'running' : r.status === 'approved' ? 'completed' : 'failed'}`}>
          {r.status}
        </span>
      </div>
      <div className="muted small">
        {r.topic && <span>topic: {r.topic} · </span>}
        {r.severity && <span>severity: {r.severity} · </span>}
        {r.risk_level && <span>level: {r.risk_level} · </span>}
        score {r.score ?? '—'} (gray zone {r.zone?.[0]}–{r.zone?.[1]})
      </div>
      {r.summary && <div className="small">{r.summary}</div>}
      {r.decision?.rationale && <div className="muted small">rationale: {r.decision.rationale}</div>}
      {r.status === 'pending' ? (
        <div className="review-actions">
          <input
            placeholder="note (optional)"
            value={notes[r.review_id] || ''}
            onChange={(e) => setNotes((n) => ({ ...n, [r.review_id]: e.target.value }))}
          />
          <button className="primary" onClick={() => resolve(r.review_id, 'approve')}>
            Approve (alert)
          </button>
          <button onClick={() => resolve(r.review_id, 'reject')}>
            Reject (suppress)
          </button>
        </div>
      ) : (
        <div className="muted small">
          → {r.final_action} {r.note ? `· note: ${r.note}` : ''} · {r.resolved_at}
        </div>
      )}
    </div>
  );

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal review-modal" onClick={(e) => e.stopPropagation()}>
        <div className="drawer-head">
          <h3 style={{ margin: 0 }}>Review — alerts in the gray zone</h3>
          <button onClick={onClose}>✕</button>
        </div>
        {error && <div className="muted small batch-error">{String(error)}</div>}
        <div className="batch-list review-list">
          {pending.map(card)}
          {pending.length === 0 && <div className="muted small">No pending reviews.</div>}
          {resolved.length > 0 && <h4>Resolved</h4>}
          {resolved.map(card)}
        </div>
      </div>
    </div>
  );
}
