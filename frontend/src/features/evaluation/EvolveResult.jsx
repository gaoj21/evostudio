import React, { useState } from 'react';
import { api } from '../../api.js';
import EvaluatorReports from './EvaluatorReports.jsx';
import JsonView from '../../components/JsonView.jsx';
import TokenUsage from '../../components/TokenUsage.jsx';
import { STAGES, elapsed, fmt } from './format.js';

function Progress({ task }) {
  const saved = ['saved_batch', 'saved_run'].includes(task.source?.type || task.params?.source?.type);
  const canvas = (task.source?.type || task.params?.source?.type) === 'canvas';
  const stages = canvas ? [] : saved ? (task.params?.mode === 'evaluate' ? ['queued', 'scoring saved results'] : ['queued', 'scoring saved results', 'proposing prompts']) : task.params?.mode === 'evaluate' ? ['queued', 'evaluating'] : STAGES;
  const reached = task.status === 'done' ? stages.length : stages.indexOf(task.stage);
  return (
    <div className="evolve-progress">
      {stages.map((name, i) => (
        <span key={name} className={`evolve-stage ${i < reached ? 'done' : i === reached && task.status === 'running' ? 'now' : ''}`}>
          {name}
        </span>
      ))}
      {/* A candidate replay names its own stage per record; show it as it comes. */}
      {task.status === 'running' && task.stage && !stages.includes(task.stage) && <span className="evolve-stage now">{task.stage}</span>}
      <span className="muted small">{task.status === 'running' ? elapsed(task) : task.status}</span>
    </div>
  );
}

// Every candidate round, whether it was taken, and why not when it was not.
function Candidates({ candidates, direction }) {
  if (!candidates?.length) return null;
  return (
    <section className="evolve-candidates">
      <h4>{candidates.length} candidate round{candidates.length === 1 ? '' : 's'} <span className="muted small">objective {direction || 'maximize'}</span></h4>
      <table>
        <thead><tr><th>Round</th><th>Score</th><th>Compared with</th><th>Prompts changed</th><th>Outcome</th></tr></thead>
        <tbody>
          {candidates.map((c) => (
            <tr key={c.round}>
              <td>{c.round}</td>
              <td>{fmt(c.metrics?.score)}</td>
              <td>{fmt(c.compared_with)}</td>
              <td>{(c.changed || []).join(', ') || '—'}</td>
              <td>{c.accepted ? 'kept' : c.comparable === false ? 'not comparable — scored fewer records' : 'rejected'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

const plural = (n, word) => `${n} ${n === 1 ? word : word.endsWith('y') ? `${word.slice(0, -1)}ies` : `${word}s`}`;

// The held-out answer: whole entities the proposer never saw and that chose
// nothing, scored once for the baseline and the chosen prompts.
function Validation({ validation }) {
  if (!validation) return null;
  const unit = validation.unit === 'record' ? 'record' : validation.unit;
  if (!validation.val_units) {
    return <p className="muted small" data-testid="validation">{validation.note}</p>;
  }
  const before = validation.baseline?.score;
  const after = validation.optimized?.score;
  const delta = before != null && after != null ? after - before : null;
  return <section className="evolve-validation" data-testid="validation">
    <h4>Held-out validation</h4>
    <div className="evolve-scores">
      <div className="evolve-score"><div className="muted small">Before</div><div className="evolve-score-value">{fmt(before)}</div></div>
      <div className="evolve-score-arrow">→</div>
      <div className="evolve-score"><div className="muted small">After</div><div className="evolve-score-value">{fmt(after)}</div></div>
      {delta != null && <span className={`evolve-delta ${delta > 0 ? 'up' : delta < 0 ? 'down' : ''}`}>{delta > 0 ? '+' : ''}{delta.toFixed(3)}</span>}
      <span className="muted small">on {plural(validation.val_units, unit)} ({plural(validation.val_records, 'record')})</span>
    </div>
    <p className="muted small" role="status">
      {!validation.changed ? 'No candidate was kept, so the held-out score is the baseline\'s.'
        : validation.improved === true ? 'The chosen prompts also score better on data they were never proposed or chosen on.'
          : validation.improved === false ? 'The chosen prompts did not score better on the held-out data: the gain may not generalize. Apply with care.'
            : 'The held-out data has no usable score (no labels among it?).'}
      {' '}Held out {Math.round((validation.val_fraction ?? 0.3) * 100)}% by a fixed split of whole entities (seed {validation.seed}); every candidate still replays everything.
    </p>
  </section>;
}

export function TaskDetail({ task, onApplied, onStopped }) {
  const [applying, setApplying] = useState(null);
  const [applyError, setApplyError] = useState(null);
  const [stopping, setStopping] = useState(false);
  const [showUnchanged, setShowUnchanged] = useState(false);
  const [showExamples, setShowExamples] = useState(false);
  const evaluationOnly = task.params?.mode === 'evaluate';
  const baseline = task.baseline?.metrics?.score;
  const optimized = task.optimized?.metrics?.score;
  const delta = baseline != null && optimized != null ? optimized - baseline : null;

  const apply = async (mode) => {
    setApplying(mode);
    setApplyError(null);
    try {
      onApplied(await api.applyEvolve(task.task_id, mode), mode);
    } catch (err) {
      setApplyError(err?.body?.detail || err.message);
    } finally {
      setApplying(null);
    }
  };

  // Stops at the next record; nothing it produced is applied.
  const stop = async () => {
    setStopping(true);
    setApplyError(null);
    try {
      await api.stopEvolve(task.task_id);
      onStopped?.(task.task_id);
    } catch (err) {
      setApplyError(err?.body?.detail || err.message);
      setStopping(false);
    }
  };

  const diffs = task.diff || [];
  const changed = diffs.filter((d) => d.changed);
  const p = task.params || {};
  const source = task.source || p.source;
  // Every score the metric returned beside the headline one, as it came.
  const { score: _score, ...otherMetrics } = task.baseline?.metrics || {};
  return (
    <div className="evolve-detail">
      <div className="muted small">
        {evaluationOnly || ['saved_batch', 'saved_run', 'canvas'].includes((task.source || p.source)?.type) ? `${task.task_id} · ${evaluationOnly ? 'Evaluation only' : (task.source || p.source)?.type === 'canvas' ? 'Canvas candidate replay' : 'Prompt proposals'} · ${p.n_dev} records · metric ${task.metric}` : <>{task.task_id} · {p.preset || 'custom'} · {p.num_candidates} candidates × {p.max_steps} rounds ·
        {' '}{p.n_train} teach / {p.n_dev} judge · metric {task.metric}</>}
        {task.elapsed_seconds != null && ` · ${elapsed(task)}`}
      </div>
      {['saved_batch', 'saved_run'].includes((task.source || p.source)?.type) && <p className="muted small">Saved results · {(task.source || p.source)?.batch_id || (task.source || p.source)?.run_id} · no workflow replay</p>}
      {source?.matched_records != null && <p className="muted small">{source.matched_records} matched records</p>}
      {task.validation_status === 'not_run' && <p role="status">Prompt suggestions are not validated. No workflow was rerun; there is no after score. {task.evidence_records} saved traces were used.</p>}
      {task.baseline?.metrics?.unscored > 0 && <p className="muted small">{task.baseline.metrics.unscored} records have no usable score. Missing labels are not treated as negative outcomes.</p>}
      {task.baseline?.report && <details><summary>Evaluation report</summary><JsonView value={task.baseline.report} startOpen={false} /></details>}
      {Object.keys(otherMetrics).length > 0 && <details><summary>Metrics</summary><JsonView value={otherMetrics} startOpen={false} /></details>}
      <EvaluatorReports reports={task.baseline?.evaluations} />
      {task.optimized?.evaluations && <><h4>Selected candidate evaluation</h4><EvaluatorReports reports={task.optimized.evaluations}/></>}
      <Candidates candidates={task.candidates} direction={task.baseline?.objective?.direction} />
      <Progress task={task} />
      <TokenUsage usage={task.token_usage} running={task.status === 'running'} />
      {task.status === 'running' && <div className="evolve-apply">
        <button type="button" onClick={stop} disabled={stopping || task.stop_requested}>{stopping || task.stop_requested ? 'Stopping…' : 'Stop'}</button>
        <span className="muted small">{stopping || task.stop_requested
          ? 'Stopping at the next record or model call. A provider request already sent finishes on its own; nothing it answers is applied.'
          : 'Stops at the next record; nothing is applied.'}</span>
      </div>}
      {task.status === 'stopped' && <p className="muted small" role="status">Stopped before it finished; nothing was applied.</p>}
      {task.status === 'interrupted' && <p className="muted small" role="status">Interrupted: the server restarted while this task was running. Start it again to get a result.</p>}
      {task.status !== 'done' && applyError && <div className="muted small batch-error">{String(applyError)}</div>}
      {task.error && <pre className="json-view batch-output batch-error">{task.error}</pre>}
      {task.status === 'done' && (
        <>
          {(baseline != null || !task.baseline?.report) && <div className="evolve-scores">
            <div className="evolve-score">
              <div className="muted small">{evaluationOnly ? 'Score' : 'Before'}</div>
              <div className="evolve-score-value">{fmt(baseline)}</div>
            </div>
            {!evaluationOnly && task.optimized && <><div className="evolve-score-arrow">→</div>
            <div className="evolve-score">
              <div className="muted small">After</div>
              <div className="evolve-score-value">{fmt(optimized)}</div>
            </div>
            </>}
            {delta != null && (
              <span className={`evolve-delta ${delta > 0 ? 'up' : delta < 0 ? 'down' : ''}`}>
                {delta > 0 ? '+' : ''}{delta.toFixed(3)}
              </span>
            )}
            <span className="muted small">{task.split
              ? `on dev: ${plural(task.split.dev_units, task.split.unit === 'record' ? 'record' : task.split.unit)} (${plural(task.split.dev_records, 'record')}) — proposed and chosen on`
              : `on the ${p.n_dev} judging record${p.n_dev === 1 ? '' : 's'}`}</span>
          </div>}
          {!evaluationOnly && <Validation validation={task.validation} />}
          {!evaluationOnly && <>
          <div className="evolve-apply">
            <button type="button" className="primary" onClick={() => apply('replace')} disabled={applying != null || changed.length === 0}>
              {applying === 'replace' ? 'Applying…' : 'Apply to this workflow'}
            </button>
            <button type="button" onClick={() => apply('new')} disabled={applying != null}>
              {applying === 'new' ? 'Saving…' : 'Save as new workflow'}
            </button>
            <span className="muted small">Apply keeps a copy named “… (before evolve)”.</span>
          </div>
          {applyError && <div className="muted small batch-error">{String(applyError)}</div>}
          <h4>{changed.length} prompt{changed.length === 1 ? '' : 's'} changed</h4>
          {changed.length === 0 && <div className="muted small">The optimizer kept every prompt as it was.</div>}
          {changed.map((d) => (
            <div className="evolve-diff" key={d.name}>
              <div className="evolve-diff-name">{d.name}</div>
              <div className="evolve-diff-cols">
                <div><div className="muted small">before</div><pre className="json-view">{d.before}</pre></div>
                <div><div className="muted small">after</div><pre className="json-view">{d.after}</pre></div>
              </div>
            </div>
          ))}
          {diffs.length > changed.length && (
            <button type="button" className="link small" onClick={() => setShowUnchanged((v) => !v)}>
              {showUnchanged ? 'Hide' : 'Show'} {diffs.length - changed.length} unchanged
            </button>
          )}
          {showUnchanged && diffs.filter((d) => !d.changed).map((d) => (
            <div className="evolve-diff" key={d.name}>
              <div className="evolve-diff-name">{d.name} <span className="muted small">{d.optimized === false ? '(not optimized)' : '(unchanged)'}</span></div>
              <pre className="json-view">{d.before}</pre>
            </div>
          ))}
          </>}
          <button type="button" className="link small" onClick={() => setShowExamples((v) => !v)}>
            {showExamples ? 'Hide' : 'Show'} the judging records
          </button>
          {showExamples && (
            <div className="batch-list">
              {Object.entries((task.optimized || task.baseline)?.records || {}).map(([id, r]) => (
                <div className="batch-item" key={id}>
                  <div className="batch-item-head">
                    <span>{id}</span>
                    <span className="muted small">
                      {!task.optimized ? `score ${fmt(r.metrics?.score)}` : `before ${fmt(task.baseline?.records?.[id]?.metrics?.score)} · after ${fmt(r.metrics?.score)}`}
                      {r.label !== undefined && ` · expected ${JSON.stringify(r.label)}`}
                      {r.reason && ` · ${r.reason}`}
                    </span>
                  </div>
                  <JsonView value={r.prediction} className="batch-output" startOpen={false} />
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

