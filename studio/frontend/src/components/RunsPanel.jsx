import React, { useEffect, useState } from 'react';
import { api } from '../api.js';
import BatchCompare from './BatchCompare.jsx';

// Runs are kept newest-first by the backend (cap 20).
function when(iso) {
  if (!iso) return '';
  const then = new Date(iso);
  const mins = Math.round((Date.now() - then.getTime()) / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  if (mins < 60 * 24) return `${Math.round(mins / 60)}h ago`;
  return then.toLocaleDateString();
}

function summarise(inputs) {
  const entries = Object.entries(inputs || {});
  if (!entries.length) return 'no inputs';
  return entries
    .slice(0, 3)
    .map(([k, v]) => `${k}=${String(v).slice(0, 24)}`)
    .join(' · ');
}

// Which pill a status gets. Batches add two states a single run has no
// equivalent for: one still winding down, and one stopped part-way.
function statusClass(status) {
  if (status === 'success' || status === 'completed') return 'completed';
  if (status === 'failed') return 'failed';
  if (status === 'cancelled' || status === 'abandoned') return 'stopped';
  return 'running';
}

// "12 records · 9 ✓ · 3 stopped" — what the batch covered, before its score.
export function describeBatch(batch) {
  const counts = batch.counts || {};
  const parts = [`${batch.total ?? 0} record${batch.total === 1 ? '' : 's'}`];
  if (counts.success) parts.push(`${counts.success} ✓`);
  if (counts.failed) parts.push(`${counts.failed} ✗`);
  if (counts.cancelled) parts.push(`${counts.cancelled} stopped`);
  return parts.join(' · ');
}

// A source the user will recognise: the file they uploaded, or the node it came
// from, rather than the internal type name.
export function describeSource(source) {
  if (!source) return '';
  if (source.filename) return source.filename;
  if (source.node) return `canvas · ${source.node}`;
  if (source.type === 'credit_risk') {
    return `credit_risk${source.split ? ` · ${source.split}` : ''}`;
  }
  return source.type || '';
}

function RunRows({ runs, onOpenRun }) {
  return (runs || []).map((run) => (
    <button
      key={run.run_id}
      type="button"
      className="run-row"
      title="Open this run on the canvas"
      onClick={() => onOpenRun(run)}
    >
      <span className={`node-status status-${statusClass(run.status)}`}>{run.status}</span>
      <span className="run-row-main">
        <span className="run-row-inputs">{summarise(run.inputs)}</span>
        {run.error && <span className="run-row-error">{String(run.error).split('\n')[0]}</span>}
      </span>
      {run.review_status && <span className="muted small">{run.review_status}</span>}
      <span className="muted small run-row-when">{when(run.created_at)}</span>
    </button>
  ));
}

function BatchRows({ batches, onOpenBatch, picked, onPick }) {
  return (batches || []).map((batch) => (
    <div className="run-row-wrap" key={batch.batch_id}>
      <input
        type="checkbox"
        className="compare-pick"
        checked={picked.includes(batch.batch_id)}
        // Two at a time: a comparison is between a before and an after.
        disabled={picked.length >= 2 && !picked.includes(batch.batch_id)}
        onChange={() => onPick(batch.batch_id)}
        title="Pick two batches to compare"
        aria-label={`Compare ${batch.batch_id}`}
      />
    <button
      type="button"
      className="run-row"
      title="Open this batch on the canvas"
      onClick={() => onOpenBatch(batch)}
    >
      <span className={`node-status status-${statusClass(batch.status)}`}>{batch.status}</span>
      <span className="run-row-main">
        <span className="run-row-inputs">{describeBatch(batch)}</span>
        <span className="muted small">{describeSource(batch.source)}</span>
      </span>
      {batch.summary?.mean != null && (
        <span className="batch-row-score" title={`${batch.metric} over ${batch.summary.scored} scored`}>
          {batch.summary.mean}
          <span className="muted small"> {batch.metric}</span>
        </span>
      )}
      <span className="muted small run-row-when">{when(batch.created_at)}</span>
    </button>
    </div>
  ));
}

export default function RunsPanel({ open, graphId, onClose, onOpenRun, onOpenBatch }) {
  const [tab, setTab] = useState('runs');
  const [runs, setRuns] = useState(null);
  const [batches, setBatches] = useState(null);
  const [error, setError] = useState(null);
  const [picked, setPicked] = useState([]);
  const [comparison, setComparison] = useState(null);
  const [comparing, setComparing] = useState(false);

  useEffect(() => {
    if (!open) return;
    setRuns(null);
    setBatches(null);
    setError(null);
    setPicked([]);
    setComparison(null);
    // Both at once: the tab strip shows how many of each there are, so it
    // cannot wait for the tab to be opened.
    Promise.all([api.listRuns(graphId), api.listBatches(graphId)])
      .then(([r, b]) => {
        setRuns(Array.isArray(r) ? r : r.runs || []);
        setBatches(Array.isArray(b) ? b : b.batches || []);
      })
      .catch((err) => setError(err?.body?.detail || err.message));
  }, [open, graphId]);

  const togglePick = (batchId) =>
    setPicked((current) => (current.includes(batchId)
      ? current.filter((id) => id !== batchId)
      : [...current, batchId].slice(0, 2)));

  const runComparison = async () => {
    const chosen = (batches || []).filter((b) => picked.includes(b.batch_id));
    if (chosen.length !== 2) return;
    // Oldest is the baseline, whichever order they were ticked in: "what did
    // my change do" only reads correctly in that direction.
    const [baseline, candidate] = [...chosen]
      .sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));
    setComparing(true);
    setError(null);
    try {
      setComparison(await api.compareBatches(candidate.batch_id, baseline.batch_id));
    } catch (err) {
      setError(err?.body?.detail || err.message);
    } finally {
      setComparing(false);
    }
  };

  if (!open) return null;

  const list = tab === 'runs' ? runs : batches;
  const empty = tab === 'runs'
    ? 'This workflow has not run yet.'
    : 'This workflow has no batch runs yet.';

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal runs-modal" onClick={(e) => e.stopPropagation()}>
        <div className="drawer-head">
          <span>History</span>
          <button type="button" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="tab-strip">
          <button
            type="button"
            className={tab === 'runs' ? 'active' : ''}
            onClick={() => setTab('runs')}
          >
            Runs{runs ? ` (${runs.length})` : ''}
          </button>
          <button
            type="button"
            className={tab === 'batches' ? 'active' : ''}
            onClick={() => setTab('batches')}
          >
            Batches{batches ? ` (${batches.length})` : ''}
          </button>
        </div>
        {error && <div className="chat-error">{String(error)}</div>}
        {comparison ? (
          <BatchCompare comparison={comparison} onBack={() => setComparison(null)} />
        ) : (
          <>
            {list === null && !error && <p className="muted small">Loading…</p>}
            {list?.length === 0 && <p className="muted small">{empty}</p>}
            {tab === 'batches' && picked.length > 0 && (
              <div className="compare-bar">
                <span className="muted small">
                  {picked.length === 1
                    ? 'Pick one more to compare against.'
                    : 'Comparing the older of these against the newer.'}
                </span>
                <button
                  type="button"
                  className="primary"
                  disabled={picked.length !== 2 || comparing}
                  onClick={runComparison}
                >
                  {comparing ? 'Comparing…' : 'Compare'}
                </button>
                <button type="button" onClick={() => setPicked([])}>Clear</button>
              </div>
            )}
            <div className="runs-list">
              {tab === 'runs'
                ? <RunRows runs={runs} onOpenRun={onOpenRun} />
                : <BatchRows batches={batches} onOpenBatch={onOpenBatch}
                             picked={picked} onPick={togglePick} />}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
