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
  const [weekday, setWeekday] = useState(0);
  const [recovery, setRecovery] = useState('ask');
  const [resumePolicy, setResumePolicy] = useState('all');
  const [zone, setZone] = useState(Intl.DateTimeFormat().resolvedOptions().timeZone);
  const [timeInput, setTimeInput] = useState('');
  const [mode, setMode] = useState('daily');
  const [time, setTime] = useState('09:00');
  const [minutes, setMinutes] = useState(60);
  const [session, setSession] = useState('');
  const [inputsText, setInputsText] = useState('{}');
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open || !graphId) return undefined;
    let active = true;
    // Start from the defaults: a workflow without a schedule must not inherit
    // the previous workflow's inputs or session.
    setError(null); setSchedule(null);
    setWeekday(0); setRecovery('ask'); setTimeInput(''); setZone(Intl.DateTimeFormat().resolvedOptions().timeZone); setMode('daily'); setTime('09:00'); setMinutes(60); setSession(''); setInputsText('{}');
    api.getSchedule(graphId).then((s) => {
      if (!active) return;
      setSchedule(s);
      if (!s.scheduled) return;
      setMode(s.mode || 'daily');
      setWeekday(s.weekday ?? 0); setRecovery(s.recovery_policy || 'ask');
      setZone(s.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone); setTimeInput(s.time_input || '');
      setTime(s.time || '09:00');
      setMinutes(s.interval_minutes || 60);
      setSession(s.session || '');
      setInputsText(JSON.stringify(s.inputs || {}, null, 2));
    }).catch((err) => { if (active) setError(err?.body?.detail || err.message); });
    return () => { active = false; };
  }, [open, graphId]);

  useEffect(() => {
    if (!open || !graphId) return undefined;
    let active = true;
    const timer = setInterval(() => {
      api.getSchedule(graphId).then(s => { if (active) setSchedule(s); }).catch(() => {});
    }, 5000);
    return () => { active = false; clearInterval(timer); };
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
        enabled, mode, time, weekday, timezone: zone, time_input: timeInput.trim(), recovery_policy: recovery, interval_minutes: Number(minutes), inputs,
        session: session.trim() || undefined,
      }));
    } catch (err) {
      setError(err?.body?.detail || err.message);
    } finally {
      setBusy(false);
    }
  };

  const resume = async () => {
    setBusy(true); setError(null);
    try { setSchedule(await api.resumeSchedule(graphId, resumePolicy)); }
    catch (err) { setError(err?.body?.detail || err.message); }
    finally { setBusy(false); }
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
          Runs the saved workflow on a timer. A Python DataLoader processes all configured
          records in batches; each occurrence finishes only after the whole batch succeeds.
        </p>

        {on && !schedule.needs_resume && (
          <div className="schedule-state">
            <span className="watch-dot" />
            {schedule.in_flight ? 'Running scheduled occurrence' : `Next run ${untilText(schedule.next_fire)}`}
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
            <option value="weekly">Every week</option>
            <option value="interval">Every N minutes</option>
          </select>
        </div>

        {mode !== 'interval' ? (
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

        {mode === 'weekly' && <div className="field"><label htmlFor="sched-day">Day</label>
          <select id="sched-day" value={weekday} onChange={e => setWeekday(Number(e.target.value))}>
            {['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'].map((day, i) => <option key={day} value={i}>{day}</option>)}
          </select></div>}
        <div className="field"><label htmlFor="sched-zone">Timezone</label><input id="sched-zone" value={zone} onChange={e => setZone(e.target.value)} /></div>
        <div className="field"><label htmlFor="sched-recovery">After downtime</label>
          <select id="sched-recovery" value={recovery} onChange={e => setRecovery(e.target.value)}>
            <option value="ask">Wait for my recovery choice</option>
            <option value="all">Run every missed occurrence in order</option>
            <option value="latest">Run only the latest missed occurrence</option>
            <option value="skip">Skip missed occurrences</option>
          </select></div>
        <div className="field"><label htmlFor="sched-time-input">Scheduled time input (optional)</label>
          <input id="sched-time-input" value={timeInput} onChange={e => setTimeInput(e.target.value)} placeholder="Input field to receive the original scheduled timestamp" />
          <p className="muted small">Your Dataset receives this field in config. The original scheduled timestamp is also available as config.run_context.scheduled_at; use it to select the period during catch-up.</p></div>
        {schedule?.scheduled && <div className="chat-note">
          <div>Experiment: {schedule.experiment_id || 'existing schedule'} · Memory is retained between occurrences.</div>
          {schedule.uses_saved_workflow && <div>Uses the workflow saved when this schedule was created. To use canvas changes, remove this schedule and create a new one.</div>}
          {schedule.in_flight?.kind === 'batch' && <div>Current batch: {schedule.in_flight.run_id}</div>}
          <div>Next planned time: {schedule.next_fire || 'not set'}</div>
          {schedule.last_completed_due && <div>Last completed period: {schedule.last_completed_due}</div>}
          {schedule.needs_resume && <div className="chat-error">Recovery needed: {schedule.last_error}</div>}
          {(!schedule.enabled || schedule.needs_resume || !schedule.running) && <>
            <label htmlFor="sched-resume">Resume strategy</label>
            <select id="sched-resume" value={resumePolicy} onChange={e => setResumePolicy(e.target.value)}>
              <option value="all">Run every missed occurrence in order</option>
              <option value="latest">Run only the latest missed occurrence</option>
              <option value="skip">Skip missed occurrences</option>
            </select>
            <button type="button" disabled={busy} onClick={resume}>Resume experiment</button>
          </>}
        </div>}
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
            {busy ? 'Saving…' : schedule?.scheduled ? 'Update' : 'Schedule it'}
          </button>
        </div>
      </div>
    </div>
  );
}
