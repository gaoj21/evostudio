import React, { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../../api.js';
import { NewTaskForm } from './EvolveForm.jsx';
import { TaskDetail } from './EvolveResult.jsx';
import { elapsed, fmt } from './format.js';
export { NewTaskForm, TaskDetail };

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
    refresh().then((list) => { if (list.length === 0) setShowForm(true); });
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

  const running = useMemo(() => tasks.filter((t) => t.status === 'running').length, [tasks]);
  if (!open) return null;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal evolve-modal" onClick={(e) => e.stopPropagation()}>
        <div className="drawer-head">
          <h3 style={{ margin: 0 }}>Evaluation & Evolve</h3>
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
              + New run
            </button>
            {running > 0 && <div className="muted small">{running} running — you can close this window.</div>}
            <div className="evolve-task-list">
              {tasks.map((t) => (
                <div
                  key={t.task_id}
                  className={`evolve-task ${t.task_id === selectedId ? 'selected' : ''}`}
                  onClick={() => select(t.task_id)}
                >
                  <div className="batch-item-head">
                    <span>{t.params?.mode === 'evaluate' ? 'Evaluation' : `Evolve · ${t.params?.preset || t.task_id}`}</span>
                    <span className={`node-status status-${t.status === 'done' ? 'completed' : t.status === 'failed' ? 'failed' : 'running'}`}>
                      {t.status === 'running' ? (t.stage || 'running') : t.status}
                    </span>
                  </div>
                  <div className="muted small">
                    {t.baseline_score != null && t.optimized_score != null
                      ? `${Number(t.baseline_score).toFixed(2)} → ${Number(t.optimized_score).toFixed(2)}`
                      : t.params?.mode === 'evaluate' && t.baseline_score != null ? `Score ${fmt(t.baseline_score)}` : t.status === 'running' ? elapsed(t) : t.metric}
                    {t.created_at && ` · ${new Date(t.created_at).toLocaleString()}`}
                  </div>
                </div>
              ))}
              {tasks.length === 0 && <div className="muted small">No evaluation or optimization runs yet.</div>}
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
              <TaskDetail task={detail} onApplied={(g, mode) => { onApplied?.(g, mode); onClose(); }} />
            ) : (
              <p className="muted">Select a run or start a new one.</p>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
