import TokenUsage from '../../components/TokenUsage.jsx';
import React, { useState } from 'react';
import { describeRun, toneClass } from './runStates.js';
import JsonView from '../../components/JsonView.jsx';

/**
 * What happened to a run, in that order: the state, the sentence, the
 * outputs, and only then — behind a click — the traceback.
 *
 * A failed run used to open on a wall of Python. The sentence the engine
 * writes (`Node 'decide' needs 'context': nothing feeds it…`) is what a
 * person acts on; the traceback is for when that is not enough.
 */
// A traceback's last line is the sentence a person can act on.
function lastLine(text) {
  const lines = String(text).trim().split('\n');
  return lines[lines.length - 1];
}

export default function RunOutcome({ run }) {
  const [showDebug, setShowDebug] = useState(false);
  const [showMemoryDebug, setShowMemoryDebug] = useState(false);
  if (!run) return null;
  const state = describeRun(run.status);
  const error = run.error || run.late_error;
  const debug = run.debug_error || run.late_debug_error;
  const node = run.node_error || run.late_node_error;
  return (
    <div className="run-outcome">
      <TokenUsage usage={run.token_usage} running={state.tone === 'running'}/>
      {state.note && <div className="muted small">{state.note}</div>}
      {run.finished_after_abandon && (
        <div className="muted small">
          It went on to <span className={`node-status ${toneClass(describeRun(run.finished_after_abandon).tone)}`}>
            {describeRun(run.finished_after_abandon).label}
          </span>.
        </div>
      )}
      {error && (
        <div className="run-error-block">
          <div className="chat-error" data-testid="run-error-summary">
            {node?.node && <span className="tool-option-name">{node.node}</span>}
            {node?.field && <> · <span className="tool-option-name">{node.field}</span></>}
            {node && ' — '}
            {String(error)}
          </div>
          {debug && (
            <button type="button" className="link small" onClick={() => setShowDebug((v) => !v)}>
              {showDebug ? 'Hide technical details' : 'Show technical details'}
            </button>
          )}
          {debug && showDebug && <pre className="json-view run-error">{String(debug)}</pre>}
        </div>
      )}
      {run.memory_error && (
        <div className="run-error-block" data-testid="run-memory-warning">
          <div className="chat-error">
            A memory read or write failed: {lastLine(run.memory_error)}
          </div>
          <button type="button" className="link small" onClick={() => setShowMemoryDebug((v) => !v)}>
            {showMemoryDebug ? 'Hide technical details' : 'Show technical details'}
          </button>
          {showMemoryDebug && <pre className="json-view run-error">{String(run.memory_error)}</pre>}
        </div>
      )}
      {(run.memory_recalled || []).filter(item => item.kind === 'mem0').map((item, index) => (
        <div key={`mem0-read-${index}`} className="muted small">Mem0 · {item.node}: retrieved {item.count} memories</div>
      ))}
      {(run.memory_written || []).filter(item => item.kind === 'mem0').map((item, index) => (
        <div key={`mem0-write-${index}`} className="muted small">Mem0 · {item.node}: wrote to shared memory</div>
      ))}
      {(run.memory_notes || []).map((note) => (
        <div key={note} className="muted small">{note}</div>
      ))}
      {run.result != null && (
        <JsonView value={run.result} />
      )}
      {run.late_result != null && (
        <>
          <div className="muted small">What it produced after you left:</div>
          <JsonView value={run.late_result} />
        </>
      )}
      {!error && run.result == null && run.late_result == null && state.tone !== 'running' && (
        <div className="muted small">No output was recorded.</div>
      )}
    </div>
  );
}
