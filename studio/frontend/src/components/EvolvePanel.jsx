import React, { useEffect, useRef, useState } from 'react';
import { api } from '../api.js';

function NewTaskForm({ graphId, onStarted, onError }) {
  const [source, setSource] = useState('upload');
  const [file, setFile] = useState(null);
  const [metrics, setMetrics] = useState([]);
  const [metric, setMetric] = useState('exact_match');
  const [sourceInfo, setSourceInfo] = useState(null);
  const [split, setSplit] = useState('');
  const [n, setN] = useState(4);
  const [seed, setSeed] = useState(42);
  const [nTrain, setNTrain] = useState(2);
  const [nDev, setNDev] = useState(2);
  const [numCandidates, setNumCandidates] = useState(2);
  const [maxSteps, setMaxSteps] = useState(2);
  const [starting, setStarting] = useState(false);

  useEffect(() => {
    api.evolveMetrics().then((r) => setMetrics(r.metrics || [])).catch(() => setMetrics([]));
    api.creditRiskSource().then(setSourceInfo).catch(() => setSourceInfo(null));
  }, []);

  const start = async (e) => {
    e.preventDefault();
    setStarting(true);
    try {
      const budget = { n_train: nTrain, n_dev: nDev, num_candidates: numCandidates, max_steps: maxSteps };
      const res =
        source === 'upload'
          ? await api.startEvolveUpload(graphId, file, { metric, ...budget })
          : await api.startEvolveSource(graphId, { metric, split: split || undefined, n: Number(n) || 4, seed: Number(seed) || 42, ...budget });
      onStarted(res.task_id);
    } catch (err) {
      onError(err?.body?.detail || err.message);
    } finally {
      setStarting(false);
    }
  };

  const splits = Object.keys(sourceInfo?.splits || {});
  const metricDesc = metrics.find((m) => m.name === metric)?.description;
  return (
    <form onSubmit={start}>
      <div className="field">
        <label>Dataset</label>
        <select value={source} onChange={(e) => setSource(e.target.value)}>
          <option value="upload">Upload JSONL ({'{inputs, label}'} per line)</option>
          <option value="credit_risk">credit_risk feed (with labels)</option>
        </select>
      </div>
      {source === 'upload' ? (
        <div className="field">
          <label>File (.jsonl)</label>
          <input type="file" accept=".jsonl" required onChange={(e) => setFile(e.target.files?.[0] || null)} />
        </div>
      ) : (
        <div className="evolve-grid">
          <div className="field">
            <label>Split</label>
            <select value={split} onChange={(e) => setSplit(e.target.value)}>
              <option value="">(all)</option>
              {splits.map((s) => (
                <option key={s} value={s}>
                  {s} ({sourceInfo.splits[s]})
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>Samples (n)</label>
            <input type="number" min="2" value={n} onChange={(e) => setN(e.target.value)} />
          </div>
          <div className="field">
            <label>Seed</label>
            <input type="number" value={seed} onChange={(e) => setSeed(e.target.value)} />
          </div>
        </div>
      )}
      <div className="field">
        <label>Metric</label>
        <select value={metric} onChange={(e) => setMetric(e.target.value)}>
          {metrics.map((m) => (
            <option key={m.name} value={m.name}>
              {m.name}
            </option>
          ))}
          {metrics.length === 0 && <option value="exact_match">exact_match</option>}
        </select>
        {metricDesc && <div className="muted small">{metricDesc}</div>}
      </div>
      <div className="evolve-grid">
        <div className="field">
          <label>Train</label>
          <input type="number" min="1" value={nTrain} onChange={(e) => setNTrain(e.target.value)} />
        </div>
        <div className="field">
          <label>Dev</label>
          <input type="number" min="1" value={nDev} onChange={(e) => setNDev(e.target.value)} />
        </div>
        <div className="field">
          <label>Candidates</label>
          <input type="number" min="1" value={numCandidates} onChange={(e) => setNumCandidates(e.target.value)} />
        </div>
        <div className="field">
          <label>Steps</label>
          <input type="number" min="1" value={maxSteps} onChange={(e) => setMaxSteps(e.target.value)} />
        </div>
      </div>
      <div className="modal-actions">
        <button type="submit" className="primary" disabled={starting || (source === 'upload' && !file)}>
          {starting ? 'Starting…' : 'Start optimization'}
        </button>
      </div>
    </form>
  );
}

function TaskDetail({ task, onApplied }) {
  const [applying, setApplying] = useState(false);
  const [applyError, setApplyError] = useState(null);
  const baseline = task.baseline?.metrics?.score;
  const optimized = task.optimized?.metrics?.score;

  const apply = async () => {
    setApplying(true);
    setApplyError(null);
    try {
      const g = await api.applyEvolve(task.task_id);
      onApplied(g);
    } catch (err) {
      setApplyError(err?.body?.detail || err.message);
    } finally {
      setApplying(false);
    }
  };

  return (
    <div>
      <div className="muted small">
        {task.task_id} · graph {task.graph_id} · metric {task.metric} · {task.status}
        {task.stage ? ` (${task.stage})` : ''}
      </div>
      {task.error && <pre className="json-view batch-output batch-error">{task.error}</pre>}
      {task.status === 'done' && (
        <>
          <div className="evolve-scores">
            <div className="evolve-score">
              <div className="muted small">Before</div>
              <div className="evolve-score-value">{baseline == null ? '—' : Number(baseline).toFixed(3)}</div>
            </div>
            <div className="evolve-score-arrow">→</div>
            <div className="evolve-score">
              <div className="muted small">After</div>
              <div className="evolve-score-value">{optimized == null ? '—' : Number(optimized).toFixed(3)}</div>
            </div>
            <button onClick={apply} disabled={applying}>
              {applying ? 'Saving…' : 'Save as new graph'}
            </button>
          </div>
          {applyError && <div className="muted small batch-error">{String(applyError)}</div>}
          <h4>Prompt diff</h4>
          {(task.diff || []).map((d) => (
            <div className="evolve-diff" key={d.name}>
              <div className="evolve-diff-name">{d.name}</div>
              <div className="evolve-diff-cols">
                <pre className="json-view">{d.before}</pre>
                <pre className="json-view">{d.after}</pre>
              </div>
            </div>
          ))}
          <h4>Per-example (dev)</h4>
          <div className="batch-list">
            {Object.entries(task.optimized?.records || {}).map(([id, r]) => (
              <div className="batch-item" key={id}>
                <div className="batch-item-head">
                  <span>{id}</span>
                  <span className="muted small">
                    score {(r.metrics?.score ?? '—')} · label {JSON.stringify(r.label)}
                  </span>
                </div>
                <pre className="json-view batch-output">{String(r.prediction)}</pre>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

export default function EvolvePanel({ open, graphId, onClose, onApplied }) {
  const [tasks, setTasks] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState(null);
  const [showForm, setShowForm] = useState(false);
  const pollRef = useRef(null);

  const refresh = async () => {
    try {
      const list = await api.listEvolveTasks(graphId);
      setTasks(list);
      return list;
    } catch {
      return [];
    }
  };

  useEffect(() => {
    if (!open) return undefined;
    refresh();
    pollRef.current = setInterval(async () => {
      const list = await refresh();
      if (selectedId) {
        const t = list.find((x) => x.task_id === selectedId);
        if (t && (t.status === 'running' || t.status !== detail?.status)) {
          api.getEvolveTask(selectedId).then(setDetail).catch(() => {});
        }
      }
    }, 3000);
    return () => clearInterval(pollRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, graphId, selectedId, detail?.status]);

  const select = async (taskId) => {
    setSelectedId(taskId);
    setShowForm(false);
    try {
      setDetail(await api.getEvolveTask(taskId));
    } catch (err) {
      setError(err?.body?.detail || err.message);
    }
  };

  if (!open) return null;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal evolve-modal" onClick={(e) => e.stopPropagation()}>
        <div className="drawer-head">
          <h3 style={{ margin: 0 }}>Evolve — prompt optimization</h3>
          <button onClick={onClose}>✕</button>
        </div>
        {error && (
          <div className="muted small batch-error" onClick={() => setError(null)}>
            {String(error)}
          </div>
        )}
        <div className="evolve-body">
          <div className="evolve-sidebar">
            <button className="primary" onClick={() => { setShowForm(true); setSelectedId(null); }} disabled={!graphId}>
              + New optimization
            </button>
            <div className="evolve-task-list">
              {tasks.map((t) => (
                <div
                  key={t.task_id}
                  className={`evolve-task ${t.task_id === selectedId ? 'selected' : ''}`}
                  onClick={() => select(t.task_id)}
                >
                  <div className="batch-item-head">
                    <span>{t.task_id}</span>
                    <span className={`node-status status-${t.status === 'done' ? 'completed' : t.status === 'failed' ? 'failed' : 'running'}`}>
                      {t.status}
                    </span>
                  </div>
                  <div className="muted small">
                    {t.metric}
                    {t.baseline_score != null && t.optimized_score != null
                      ? ` · ${Number(t.baseline_score).toFixed(2)} → ${Number(t.optimized_score).toFixed(2)}`
                      : ''}
                  </div>
                </div>
              ))}
              {tasks.length === 0 && <div className="muted small">No optimization tasks yet.</div>}
            </div>
          </div>
          <div className="evolve-main">
            {showForm ? (
              <NewTaskForm
                graphId={graphId}
                onStarted={(id) => { setShowForm(false); select(id); }}
                onError={setError}
              />
            ) : detail ? (
              <TaskDetail task={detail} onApplied={(g) => { onApplied?.(g); onClose(); }} />
            ) : (
              <p className="muted">Select a task or start a new optimization.</p>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
