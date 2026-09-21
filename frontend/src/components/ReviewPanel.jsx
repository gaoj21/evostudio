import React, { useEffect, useRef, useState } from 'react';
import { api } from '../api.js';

// Bookkeeping every review carries; the rest is what the reviewer judges.
const REVIEW_META = new Set(['review_id', 'run_id', 'graph_id', 'status', 'node', 'score', 'zone', 'fields', 'output',
  'approve_label', 'reject_label', 'final_action', 'note', 'created_at', 'resolved_at', 'resolution']);

const shown = (value) => (value !== null && typeof value === 'object' ? JSON.stringify(value) : String(value));

// The fields the review rule chose to show; a review saved before rules had
// them shows whatever other keys it has, as they are.
export function reviewFields(review) {
  const source = review.fields && typeof review.fields === 'object'
    ? review.fields
    : Object.fromEntries(Object.entries(review).filter(([key]) => !REVIEW_META.has(key)));
  return Object.entries(source)
    .filter(([, value]) => value !== undefined && value !== null && value !== '')
    .map(([key, value]) => [key, shown(value)]);
}

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

  const card = (r) => {
    const approve = r.approve_label || 'approved';
    const reject = r.reject_label || 'rejected';
    return (
      <div className="batch-item" key={r.review_id}>
        <div className="batch-item-head">
          <span>{r.node || 'Output'}{r.run_id ? ` · run ${r.run_id}` : ''}</span>
          <span className={`node-status status-${r.status === 'pending' ? 'running' : r.status === 'approved' ? 'completed' : 'failed'}`}>
            {r.status}
          </span>
        </div>
        {(r.score != null || r.zone) && <div className="muted small">
          score {r.score ?? '—'}{r.zone ? ` (review band ${r.zone[0]}–${r.zone[1]})` : ''}
        </div>}
        {reviewFields(r).map(([key, value]) => (
          <div className="small" key={key}><span className="muted">{key}:</span> {value}</div>
        ))}
        {r.status === 'pending' ? (
          <div className="review-actions">
            <input
              placeholder="note (optional)"
              value={notes[r.review_id] || ''}
              onChange={(e) => setNotes((n) => ({ ...n, [r.review_id]: e.target.value }))}
            />
            <button className="primary" onClick={() => resolve(r.review_id, 'approve')}>
              Approve ({approve})
            </button>
            <button onClick={() => resolve(r.review_id, 'reject')}>
              Reject ({reject})
            </button>
          </div>
        ) : (
          <div className="muted small">
            → {r.final_action} {r.note ? `· note: ${r.note}` : ''} · {r.resolved_at}
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal review-modal" onClick={(e) => e.stopPropagation()}>
        <div className="drawer-head">
          <h3 style={{ margin: 0 }}>Review — outputs waiting for a decision</h3>
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
