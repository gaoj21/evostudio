import React, { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api.js';

const HISTORY_KEY = 'evoagentx-studio:chat';
// Enough to rebuild the conversation on reload without letting one long
// session grow localStorage without bound.
const KEEP_MESSAGES = 40;

const SUGGESTIONS = [
  'Build a workflow that monitors supplier credit risk from news',
  'Add a step that summarises the findings for an analyst',
  'Make the detect node return JSON instead of prose',
];

let seq = 0;
const newId = () => `m${Date.now().toString(36)}${(seq += 1)}`;

// Whole-workflow generation runs as a backend job; poll until it settles.
const POLL_MS = 2000;
const POLL_TIMEOUT_MS = 10 * 60 * 1000;

function loadHistory(graphId) {
  if (!graphId) return [];
  try {
    const saved = JSON.parse(localStorage.getItem(`${HISTORY_KEY}:${graphId}`)) || [];
    // Conversations stored before messages carried ids still need a stable key.
    return saved.map((m) => (m.id ? m : { ...m, id: newId() }));
  } catch {
    return [];
  }
}

function saveHistory(graphId, messages) {
  if (!graphId) return;
  try {
    localStorage.setItem(
      `${HISTORY_KEY}:${graphId}`,
      JSON.stringify(messages.slice(-KEEP_MESSAGES))
    );
  } catch {
    /* storage unavailable; the conversation still works for this session */
  }
}

// "3 nodes added · 2 edges · re-laid out" — a glanceable summary of what the
// turn actually did to the canvas.
// Renaming a workflow renames its id, and the conversation is filed under it.
// Without this the chat would look wiped the moment the workflow is named.
export function renameChatHistory(oldId, newId) {
  try {
    const saved = localStorage.getItem(`${HISTORY_KEY}:${oldId}`);
    if (saved === null) return;
    localStorage.setItem(`${HISTORY_KEY}:${newId}`, saved);
    localStorage.removeItem(`${HISTORY_KEY}:${oldId}`);
  } catch {
    /* storage unavailable; the conversation just starts fresh */
  }
}


export function summarise(operations) {
  const counts = {};
  (operations || []).forEach((o) => {
    counts[o.op] = (counts[o.op] || 0) + 1;
  });
  const label = {
    add_node: 'added',
    update_node: 'updated',
    delete_node: 'deleted',
    rename_node: 'renamed',
    add_edge: 'edge',
    delete_edge: 'edge removed',
    generate_workflow: 'generated workflow',
    create_tool: 'created tool',
    delete_tool: 'deleted tool',
    create_skill: 'created skill',
    delete_skill: 'deleted skill',
    auto_layout: 're-laid out',
    set_goal: 'goal set',
    set_name: 'renamed graph',
    set_output_dir: 'output dir set',
    save_graph: 'saved',
    run_workflow: 'proposed a run',
    validate: 'checked',
    inspect_node: 'read a node',
    read_run: 'read the run',
    list_runs: 'listed runs',
    read_file: 'read a file',
    list_files: 'listed files',
    write_file: 'wrote a file',
    delete_file: 'deleted a file',
    make_dir: 'made a folder',
  };
  return Object.entries(counts)
    .map(([op, n]) => (n > 1 ? `${label[op] || op} ×${n}` : label[op] || op))
    .join(' · ');
}

export default function ChatPanel({
  graphId,
  getGraph,
  onApply,
  onSaved,
  onRunRequest,
  runOutcome,
  disabled,
}) {
  const [messages, setMessages] = useState(() => loadHistory(graphId));
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const listRef = useRef(null);
  const busyRef = useRef(false);
  const messagesRef = useRef(messages);
  messagesRef.current = messages;
  // Generation job this panel has already picked up, so a re-render does not
  // attach to it a second time.
  const attachedJob = useRef(null);
  // The re-attach effect reads these instead of depending on them: both change
  // identity as the canvas changes, and a dependency on either re-ran it.
  const pollJobRef = useRef(null);
  const getGraphRef = useRef(getGraph);
  getGraphRef.current = getGraph;
  // Which graph is open right now. A generation started minutes ago must not
  // drop its result onto a different canvas if the user switched graphs.
  const activeGraph = useRef(graphId);
  activeGraph.current = graphId;
  // Run this panel started and is still waiting on, so the outcome is reported
  // back to the agent exactly once.
  const awaitingRun = useRef(null);

  // Each graph keeps its own conversation: the model is always reasoning about
  // one specific canvas, so carrying messages across graphs would mislead it.
  useEffect(() => {
    setMessages(loadHistory(graphId));
    setInput('');
  }, [graphId]);

  useEffect(() => {
    saveHistory(graphId, messages);
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [graphId, messages]);


  // Generation reports its stage while it runs; the bubble created for the turn
  // is updated in place until the job settles.
  const pollJob = useCallback(
    async (jobId, messageId, before) => {
      const patch = (fields) =>
        setMessages((m) => m.map((msg) => (msg.id === messageId ? { ...msg, ...fields } : msg)));
      const deadline = Date.now() + POLL_TIMEOUT_MS;
      for (;;) {
        await new Promise((r) => setTimeout(r, POLL_MS));
        if (Date.now() > deadline) {
          patch({ stage: '', errors: ['Generation timed out.'] });
          return;
        }
        let job;
        try {
          job = await api.chatJob(graphId, jobId);
        } catch (err) {
          patch({ stage: '', errors: [`Lost track of the generation job: ${err.message}`] });
          return;
        }
        if (job.status === 'running') {
          patch({ stage: `${job.stage} (${job.elapsed}s)` });
          continue;
        }
        if (job.status === 'done' && job.graph) {
          if (activeGraph.current !== graphId) {
            patch({
              stage: '',
              notes: ['Generation finished after you switched workflows, so it '
                + 'was not applied. Ask again on this canvas to use it.'],
            });
            return;
          }
          onApply(job.graph);
          patch({
            stage: '',
            summary: `generated ${(job.graph.tasks || []).length} nodes in ${job.elapsed}s`,
            snapshot: before,
          });
        } else {
          patch({ stage: '', errors: [job.error || 'Generation failed.'] });
        }
        return;
      }
    },
    [graphId, onApply]
  );

  // A generation outlives the page: it runs on the server for minutes, so a
  // reload used to leave it invisible — and asking again just started a second
  // one. Re-attach to whatever is still running for this workflow.
  //
  // Keyed on the workflow alone, with the callbacks reached through refs: they
  // change identity whenever the canvas does, and depending on them attached
  // the same job again on every edit.
  useEffect(() => {
    if (!graphId) return undefined;
    let cancelled = false;
    api
      .runningChatJob(graphId)
      .then((job) => {
        if (cancelled || !job?.job_id || busyRef.current) return;
        if (attachedJob.current === job.job_id) return;
        attachedJob.current = job.job_id;
        const id = newId();
        setMessages((m) => [...m, {
          id,
          role: 'assistant',
          content: 'Picking up the workflow generation that was already running.',
          stage: `${job.stage} (${job.elapsed}s)`,
        }]);
        busyRef.current = true;
        setBusy(true);
        pollJobRef.current(job.job_id, id, getGraphRef.current())
          .finally(() => {
            busyRef.current = false;
            setBusy(false);
          });
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [graphId]);

  const submit = useCallback(async (text, { silent = false } = {}) => {
    if (!text || busyRef.current || !graphId) return;
    const before = getGraph();
    const history = messagesRef.current.map((m) => ({ role: m.role, content: m.content }));

    setMessages((m) => [...m, { id: newId(), role: 'user', content: text, silent }]);
    busyRef.current = true;
    setBusy(true);
    try {
      const res = await api.chatGraph(graphId, { message: text, history, graph: before });
      if (res.applied && res.graph) onApply(res.graph);
      if (res.saved) onSaved && onSaved();
      const id = newId();
      setMessages((m) => [
        ...m,
        {
          id,
          role: 'assistant',
          content: res.reply || '',
          summary: summarise(res.operations),
          errors: res.errors || [],
          notes: res.notes || [],
          tools: res.tools_created || [],
          // The canvas as it was before this turn, so the edit can be undone
          // without a global undo stack.
          snapshot: res.applied ? before : null,
          stage: res.job_id ? 'Starting…' : '',
          saved: !!res.saved,
          // A run the model proposed: it does not start until the user says so.
          pendingRun: res.pending_run || null,
        },
      ]);
      if (res.job_id) await pollJob(res.job_id, id, before);
    } catch (err) {
      const detail = err?.body?.detail;
      setMessages((m) => [
        ...m,
        {
          id: newId(),
          role: 'assistant',
          content: '',
          errors: [typeof detail === 'string' ? detail : err.message],
        },
      ]);
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }, [graphId, getGraph, onApply, onSaved, pollJob]);

  pollJobRef.current = pollJob;

  const send = useCallback(() => {
    const text = input.trim();
    if (!text) return;
    setInput('');
    submit(text);
  }, [input, submit]);

  // When a run this panel started settles, hand the agent its outcome so it can
  // diagnose without the user having to ask. One follow-up per run.
  useEffect(() => {
    const runId = awaitingRun.current;
    if (!runOutcome || !runId || runOutcome.run_id !== runId || busy) return;
    awaitingRun.current = null;
    submit(
      `The run you started (${runId}) finished with status "${runOutcome.status}". `
        + 'Read it and tell me what happened. If it failed or a node produced the '
        + 'wrong thing, diagnose it and fix the workflow.',
      { silent: true }
    );
  }, [runOutcome, busy, submit]);

  const confirmRun = useCallback(
    async (id) => {
      const message = messagesRef.current.find((m) => m.id === id);
      if (!message?.pendingRun) return;
      const patch = (fields) =>
        setMessages((m) => m.map((msg) => (msg.id === id ? { ...msg, ...fields } : msg)));
      try {
        // Saves the canvas first, so the run matches what is on screen.
        const runId = await onRunRequest(message.pendingRun.inputs || {});
        awaitingRun.current = runId || null;
        patch({ pendingRun: null, runStarted: runId || 'started' });
      } catch (err) {
        patch({ pendingRun: null, errors: [err?.body?.detail || err.message] });
      }
    },
    [onRunRequest]
  );

  const dismissRun = useCallback((id) => {
    setMessages((m) =>
      m.map((msg) => (msg.id === id ? { ...msg, pendingRun: null, runDeclined: true } : msg))
    );
  }, []);

  const undo = useCallback(
    (id) => {
      const message = messages.find((m) => m.id === id);
      if (!message?.snapshot) return;
      onApply(message.snapshot);
      setMessages((m) =>
        m.map((msg) => (msg.id === id ? { ...msg, snapshot: null, undone: true } : msg))
      );
    },
    [messages, onApply]
  );

  if (!graphId) {
    return <div className="chat-panel"><div className="muted small">Open a workflow to start.</div></div>;
  }

  return (
    <div className="chat-panel">
      <div className="chat-head">
        <span>CHAT</span>
        <button
          type="button"
          className="chat-clear"
          title="Clear this conversation"
          disabled={!messages.length || busy}
          onClick={() => setMessages([])}
        >
          Clear
        </button>
      </div>

      <div className="chat-messages" ref={listRef}>
        {messages.length === 0 && (
          <div className="chat-empty">
            <p className="muted small">
              Describe the workflow you want, or ask for a change to the one on the canvas.
              Edits apply straight to the canvas — undo any of them from here, and save when
              you are happy.
            </p>
            {SUGGESTIONS.map((s) => (
              <button key={s} type="button" className="chat-suggestion" onClick={() => setInput(s)}>
                {s}
              </button>
            ))}
          </div>
        )}

        {messages.map((m) => (
          <div key={m.id} className={`chat-msg chat-${m.role}${m.silent ? ' chat-auto' : ''}`}>
            {m.content && <div className="chat-bubble">{m.content}</div>}
            {m.summary && <div className="chat-summary">✓ {m.summary}</div>}
            {m.saved && <div className="chat-summary">💾 saved to disk</div>}
            {m.pendingRun && (
              <div className="chat-confirm">
                <div className="chat-note">
                  Run this workflow?{' '}
                  {Object.keys(m.pendingRun.inputs || {}).length
                    ? Object.entries(m.pendingRun.inputs)
                        .map(([k, v]) => `${k}=${String(v).slice(0, 40)}`)
                        .join(' · ')
                    : 'no inputs'}
                </div>
                <div className="chat-confirm-actions">
                  <button type="button" className="primary" onClick={() => confirmRun(m.id)}>
                    Run it
                  </button>
                  <button type="button" onClick={() => dismissRun(m.id)}>
                    Not now
                  </button>
                </div>
              </div>
            )}
            {m.runStarted && <div className="chat-summary">▶ run {m.runStarted} started</div>}
            {m.runDeclined && <div className="chat-note">Run declined.</div>}
            {(m.tools || []).length > 0 && (
              <div className="chat-summary">🔧 tool created: {m.tools.join(', ')}</div>
            )}
            {(m.notes || []).map((n, j) => (
              <div key={j} className="chat-note">{n}</div>
            ))}
            {(m.errors || []).map((e, j) => (
              <div key={j} className="chat-error">{e}</div>
            ))}
            {m.stage && <div className="chat-thinking">{m.stage}</div>}
            {m.snapshot && (
              <button type="button" className="chat-undo" onClick={() => undo(m.id)}>
                Undo this change
              </button>
            )}
            {m.undone && <div className="chat-note">Change undone.</div>}
          </div>
        ))}

        {busy && <div className="chat-msg chat-assistant"><div className="chat-thinking">Thinking…</div></div>}
      </div>

      <div className="chat-input">
        <textarea
          rows={3}
          placeholder={disabled ? 'Chat is unavailable right now' : 'Describe a workflow, a change, or ask it to run and debug…'}
          value={input}
          disabled={busy || disabled}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
        />
        <button type="button" className="primary" onClick={send} disabled={busy || disabled || !input.trim()}>
          {busy ? 'Working…' : 'Send'}
        </button>
      </div>
    </div>
  );
}
