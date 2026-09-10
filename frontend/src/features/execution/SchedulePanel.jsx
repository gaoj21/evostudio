import React, { useEffect, useState } from 'react';
import { api } from '../../api.js';

export const MIN_INTERVAL_MINUTES = 5;

// "in 3h 20m" — near enough, and it answers the only question the number is
// there to answer.
export function untilText(iso, now = Date.now()) {
  if (!iso) return '';
  const due = new Date(iso).getTime();
  if (Number.isNaN(due)) return '';
  const mins = Math.round((due - now) / 60000);
  if (mins < 0) return 'overdue';
  if (mins < 1) return 'in under a minute';
  if (mins < 60) return `in ${mins}m`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `in ${hours}h ${mins % 60}m`;
  return `in ${Math.round(hours / 24)}d`;
}

/**
 * Running a workflow on a timer.
 *
 * A schedule fires whether or not anything changed — unlike Watch, which polls
 * a source and runs only when new data turns up.
 */
export default function SchedulePanel({ open, graphId, onClose }) {
  const [schedule, setSchedule] = useState(null);
  const [mode, setMode] = useState('daily');
  const [time, setTime] = useState('09:00');
  const [minutes, setMinutes] = useState(60);
  const [session, setSession] = useState('');
  const [inputsText, setInputsText] = useState('{}');
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open || !graphId) return;
    setError(null);
    api.getSchedule(graphId).then((s) => {
      setSchedule(s);
      if (!s.scheduled) return;
      setMode(s.mode || 'daily');
      setTime(s.time || '09:00');
      setMinutes(s.interval_minutes || 60);
      setSession(s.session || '');
      setInputsText(JSON.stringify(s.inputs || {}, null, 2));
    }).catch((err) => setError(err?.body?.detail || err.message));
  }, [open, graphId]);

  if (!open) return null;

  const save = async (enabled) => {
    let inputs;
    try {
      inputs = JSON.parse(inputsText || '{}');
    } catch (e) {
      setError(`Inputs must be JSON: ${e.message}`);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      setSchedule(await api.setSchedule(graphId, {
        enabled, mode, time, interval_minutes: Number(minutes), inputs,
        session: session.trim() || undefined,
      }));
    } catch (err) {
      setError(err?.body?.detail || err.message);
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    setBusy(true);
    try {
      await api.clearSchedule(graphId);
      setSchedule({ graph_id: graphId, scheduled: false });
    } catch (err) {
      setError(err?.body?.detail || err.message);
    } finally {
      setBusy(false);
    }
  };

  const on = schedule?.scheduled && schedule?.enabled;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="drawer-head">
          <span>Schedule</span>
          <button type="button" onClick={onClose}>✕</button>
        </div>

        <p className="muted small">
          Runs this workflow on a timer, whether or not anything changed. Each fire
          is an ordinary run: it appears in run history and costs what a run costs.
        </p>

        {on && (
          <div className="schedule-state">
            <span className="watch-dot" />
            Next run {untilText(schedule.next_fire)}
            <span className="muted small">
              {' · '}{schedule.fires} so far
              {schedule.last_error ? ` · last failed: ${schedule.last_error}` : ''}
            </span>
          </div>
        )}

        <div className="field">
          <label htmlFor="sched-mode">How often</label>
          <select id="sched-mode" value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="daily">Every day at a time</option>
            <option value="interval">Every N minutes</option>
          </select>
        </div>

        {mode === 'daily' ? (
          <div className="field">
            <label htmlFor="sched-time">Time</label>
            <input
              id="sched-time"
              type="time"
              value={time}
              onChange={(e) => setTime(e.target.value)}
            />
          </div>
        ) : (
          <div className="field">
            <label htmlFor="sched-minutes">Minutes between runs</label>
            <input
              id="sched-minutes"
              type="number"
              min={MIN_INTERVAL_MINUTES}
              value={minutes}
              onChange={(e) => setMinutes(e.target.value)}
            />
            <div className="muted small">
              At least {MIN_INTERVAL_MINUTES} — nobody is watching these run.
            </div>
          </div>
        )}

        <div className="field">
          <label htmlFor="sched-inputs">Inputs <span className="muted">(JSON)</span></label>
          <textarea
            id="sched-inputs"
            rows={4}
            className="code-input"
            value={inputsText}
            onChange={(e) => setInputsText(e.target.value)}
          />
        </div>

        <div className="field">
          <label htmlFor="sched-session">Session <span className="muted">(optional)</span></label>
          <input
            id="sched-session"
            value={session}
            onChange={(e) => setSession(e.target.value)}
            placeholder="e.g. daily-report"
          />
          <div className="muted small">
            Give every fire the same session and each run is told what the
            previous ones did.
          </div>
        </div>

        {error && <div className="chat-error">{String(error)}</div>}

        <div className="modal-actions">
          {schedule?.scheduled && (
            <button type="button" className="danger-ghost" onClick={remove} disabled={busy}>
              Remove
            </button>
          )}
          {on ? (
            <button type="button" onClick={() => save(false)} disabled={busy}>
              Pause
            </button>
          ) : null}
          <button type="button" className="primary" onClick={() => save(true)} disabled={busy}>
            {busy ? 'Saving…' : on ? 'Update' : 'Schedule it'}
          </button>
        </div>
      </div>
    </div>
  );
}
