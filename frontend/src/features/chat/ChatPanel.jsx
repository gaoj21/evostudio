import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useChatCancellation } from './useChatCancellation.js';
import ChatSessions from './ChatSessions.jsx';
import ChatActivity from './ChatActivity.jsx';
import ChatComposer from './ChatComposer.jsx';
import { useChatHistory, chatHistoryKey, loadChatHistory, saveChatHistory, renameChatHistoryKey, newMessageId } from './chatSession.js';
import { followTurn, loadPendingTurn, savePendingTurn, clearPendingTurn, rememberStopped, wasStopped, turnOutcome } from './assistantTurns.js';
import CopyButton from './CopyButton.jsx';
import WorkflowRunCard from '../execution/WorkflowRunCard.jsx';
import { api } from '../../api.js';
import { RUN_SETTLED } from '../execution/runStates.js';

const SUGGESTIONS = [
  'Build a workflow that summarises incoming support tickets by topic',
  'Add a step that summarises the findings for an analyst',
  'Check required inputs and prepare a run',
  'Inspect the latest failed run and explain what to fix',
  'Show this workflow’s recent runs',
];

const newId = newMessageId;

// Whole-workflow generation runs as a backend job; poll until it settles.
const POLL_MS = 2000;
const POLL_TIMEOUT_MS = 10 * 60 * 1000;

const loadHistory = graphId => graphId ? loadChatHistory(chatHistoryKey(graphId)) : [];
const saveHistory = (graphId, messages) => { if (graphId) saveChatHistory(chatHistoryKey(graphId), messages); };
export function renameChatHistory(oldId, newId) {
  renameChatHistoryKey(chatHistoryKey(oldId), chatHistoryKey(newId));
}

export function summarise(operations) {
  const counts = {};
  // A Chat Agent change says what it did ("connected 'X' to memory 'Y'"),
  // in the order it happened; everything else is counted by kind.
  const described = [];
  (operations || []).forEach((o) => {
    if (o.summary) { described.push(o.summary); return; }
    counts[o.op] = (counts[o.op] || 0) + 1;
  });
  const label = {
    add_node: 'added',
    update_node: 'updated',
    delete_node: 'deleted',
    rename_node: 'renamed',
    add_edge: 'edge',
    delete_edge: 'edge removed',
    create_agent: 'created Chat Agent',
    delete_agent: 'removed Chat Agent',
    update_agent: 'updated Chat Agent',
    inspect_agent: 'read a Chat Agent',
    list_agents: 'listed Chat Agents',
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
  return [...described, ...Object.entries(counts)
    .map(([op, n]) => (n > 1 ? `${label[op] || op} ×${n}` : label[op] || op))]
    .join(' · ');
}

// Operations that edit the canvas graph, and the Chat Agent shortcuts (which
// the server reports as the canvas operations they amount to).
const CANVAS_OPS = new Set(['add_node', 'update_node', 'delete_node', 'rename_node', 'add_edge',
  'delete_edge', 'set_goal', 'set_name', 'auto_layout', 'configure_workflow', 'set_output_dir']);
const AGENT_OPS = new Set(['create_agent', 'delete_agent', 'update_agent', 'connect_agent_memory',
  'disconnect_agent_memory', 'add_agent_tool', 'remove_agent_tool', 'add_agent_skill', 'remove_agent_skill']);

// What a turn actually did. A server that reports the canvas edits that took
// effect is believed over the operations the model asked for: a delete that
// matched nothing must not read as "deleted".
export function appliedOperations(res) {
  if (!Array.isArray(res?.canvas_operations)) return res?.operations || [];
  const others = (res.operations || []).filter(o => !CANVAS_OPS.has(o?.op) && !AGENT_OPS.has(o?.op));
  return [...res.canvas_operations, ...others];
}

// The canvas as a comparable value (Chat Agents are among its tasks).
export function canvasKey(graph) {
  if (!graph) return '';
  return JSON.stringify(graph);
}

// What a generated workflow would replace: the nodes, their settings and the
// edges — not where the nodes sit. Dragging a node while a generation runs is
// no reason to hold its result back.
export function contentKey(graph) {
  if (!graph) return '';
  const { memory_positions: _positions, tasks, ...rest } = graph;
  return JSON.stringify({ ...rest, tasks: (tasks || []).map(({ x: _x, y: _y, ...task }) => task) });
}

// Generating a workflow onto an empty canvas has nothing to overwrite.
export function generationWouldReplaceWork(before, current) {
  if (!current || !(current.tasks || []).length) return false;
  return contentKey(current) !== contentKey(before);
}

export function conversationHistory(messages) {
  return messages.map(m => ({ role: m.role, content: [m.content,
    m.role === 'assistant' && m.summary ? `Applied changes: ${m.summary}` : '',
    m.errors?.length ? `Operation failed: ${m.errors.join('; ')}` : '',
    m.notes?.length ? `Notes: ${m.notes.join('; ')}` : '',
    m.runStarted ? `Run ${m.runStarted}: ${m.runStatus || 'started'}` : '',
    m.undone ? 'These changes were subsequently undone.' : '',
  ].filter(Boolean).join('\n') })).filter(m => m.content);
}

export default function ChatPanel({
  graphId,
  getGraph,
  onApply,
  onSaved,
  onRunRequest,
  onOpenPanel,
  runOutcome,
  disabled,
}) {
  const cancellation = useChatCancellation(graphId);
  const [messages, setMessages, sessions] = useChatHistory(chatHistoryKey(graphId));
  const [input, setInput] = useState('');
  useEffect(() => { setInput(''); }, [sessions.activeId]);
  const [busy, setBusy] = useState(false);
  // Progress of the turn running on the server ("Thinking… (12s)").
  const [progress, setProgress] = useState('');
  const turnStore = chatHistoryKey(graphId);
  const { updateSession, messagesOf } = sessions;
  const sessionsRef = useRef(sessions);
  sessionsRef.current = sessions;
  const [startingRun, setStartingRun] = useState(false);
  const [liveRun, setLiveRun] = useState(null);
  const [runError, setRunError] = useState('');
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
  useEffect(() => { activeGraph.current = graphId; return () => { activeGraph.current = null; }; }, [graphId]);
  // Run this panel started and is still waiting on, so the outcome is reported
  // back to the agent exactly once.
  const awaitingRun = useRef(null);

  useEffect(() => {
    const restore = event => { if (event.detail === graphId) setMessages(loadHistory(graphId)); };
    window.addEventListener('workflow-chat-history', restore);
    return () => window.removeEventListener('workflow-chat-history', restore);
  }, [graphId, setMessages]);

  // Each graph keeps its own conversation: the model is always reasoning about
  // one specific canvas, so carrying messages across graphs would mislead it.
  useEffect(() => {
    setInput('');
  }, [graphId]);

  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [graphId, messages]);


  // Generation reports its stage while it runs; the bubble created for the turn
  // is updated in place until the job settles.
  const pollJob = useCallback(
    async (jobId, messageId, before) => {
      const turn = cancellation.attachJob(jobId);
      const patch = (fields) =>
        setMessages((m) => m.map((msg) => (msg.id === messageId ? { ...msg, ...fields } : msg)));
      const deadline = Date.now() + POLL_TIMEOUT_MS;
      for (;;) {
        await new Promise((r) => setTimeout(r, POLL_MS));
        if (turn.stopped) return;
        if (activeGraph.current !== graphId) {
          // The job keeps running on the server; reopening this workflow
          // re-attaches to it.
          patch({ stage: '', notes: ['You switched workflows while this was generating. '
            + 'It keeps running; open this workflow again to pick it up.'] });
          return;
        }
        // A message sent meanwhile does not drop the generation: its result
        // is offered instead of applied (below), as that turn may change the
        // canvas too.
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
        if (turn.stopped) return;
        if (job.status === 'cancelled') { patch({ stage: '', content: 'Stopped.', stopped: true }); return; }
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
          const summary = `generated ${(job.graph.tasks || []).length} nodes in ${job.elapsed}s`;
          // Generation returns a whole new workflow, not edits that could be
          // replayed. If the person changed the canvas while it ran, replacing
          // it would silently throw their work away: ask first.
          const current = getGraphRef.current?.();
          const superseded = !cancellation.isCurrent(turn);
          if (superseded || generationWouldReplaceWork(before, current)) {
            patch({ stage: '', pendingGraph: job.graph, pendingSummary: summary,
              notes: job.graph.migration_warnings || [] });
            return;
          }
          onApply(job.graph);
          patch({
            stage: '',
            summary,
            snapshot: before,
            notes: job.graph.migration_warnings || [],
          });
        } else {
          patch({ stage: '', summary: '', errors: [job.error || 'Generation failed.'] });
        }
        return;
      }
    },
    [graphId, onApply, setMessages, cancellation.attachJob, cancellation.isCurrent]
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
        const turn = cancellation.attachJob(job.job_id);
        pollJobRef.current(job.job_id, id, getGraphRef.current())
          .finally(() => {
            if (cancellation.isCurrent(turn)) {
              busyRef.current = false;
              setBusy(false);
              cancellation.finish(turn);
            }
          });
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [graphId, setMessages, cancellation.attachJob, cancellation.isCurrent, cancellation.finish]);

  // The id is made outside the state updater: React may run an updater more
  // than once, and a fresh id each time would remount the message.
  const appendTo = useCallback((sessionId, message) => {
    const row = { id: newId(), ...message };
    updateSession(sessionId, m => [...m, row]);
  }, [updateSession]);
  // What a settled turn means for its conversation: the reply, the canvas it
  // produced and any generation it handed off. Lands in the session the turn
  // was asked in, even if the chat was closed and reopened meanwhile.
  const settle = useCallback(async (res, { sessionId, before }) => {
    const id = newId();
    let graphToApply = (res.applied || res.saved) && res.graph ? res.graph : null;
    let base = before;
    let notes = res.notes || [];
    let errors = res.errors || [];
    let summary = res.job_id || errors.length ? '' : summarise(appliedOperations(res));
    // The turn worked on the canvas sent with the message. If the person
    // edited it meanwhile, its result would wipe their edits: replay the
    // turn's canvas edits onto the canvas as it is now instead.
    const current = graphToApply && before ? getGraphRef.current?.() : null;
    if (current && canvasKey(current) !== canvasKey(before)) {
      const operations = res.canvas_operations || [];
      let replayed = null;
      if (operations.length && api.replayChatOperations) {
        try {
          replayed = await api.replayChatOperations(graphId, { graph: current, operations });
        } catch (err) {
          replayed = { applied: false, errors: [err?.body?.detail || err.message] };
        }
      }
      if (replayed?.applied) {
        graphToApply = { ...replayed.graph, id: res.graph.id ?? replayed.graph.id };
        base = current;
        notes = [...notes, 'The canvas changed while I was working, so my edits were applied on top of your changes.'];
      } else {
        graphToApply = null;
        summary = '';
        errors = [...errors, 'The canvas changed while I was working, so my edits were not applied, to keep yours. Ask again to redo them on the current canvas.',
          ...(replayed?.errors || []), ...(replayed?.notes || [])];
      }
    }
    const nextMessages = [
      ...messagesOf(sessionId),
      {
        id,
        role: 'assistant',
        content: res.reply || '',
        activity: res.activity || [],
        summary,
        errors,
        notes,
        tools: res.tools_created || [],
        // The canvas as it was before this turn, so the edit can be undone
        // without a global undo stack.
        snapshot: graphToApply && res.applied && base ? { ...base, id: res.graph?.id || base.id } : null,
        stage: res.job_id ? 'Starting…' : '',
        saved: !!res.saved,
        // A run the model proposed: it does not start until the user says so.
        pendingRun: res.pending_run || null,
      },
    ];
    updateSession(sessionId, nextMessages);
    if (graphToApply) onApply(graphToApply);
    if (res.saved && res.graph?.id && res.graph.id !== graphId) saveHistory(res.graph.id, nextMessages);
    if (res.saved) onSaved && onSaved();
    if (res.panel) onOpenPanel?.(res.panel);
    if (res.job_id) await pollJob(res.job_id, id, before);
  }, [graphId, onApply, onSaved, onOpenPanel, pollJob, updateSession, messagesOf]);

  // Follow a turn running on the server until it settles. Leaving the chat
  // only stops watching: the turn keeps going and the next mount re-attaches.
  const drive = useCallback(async (turn, pending) => {
    const known = sessionsRef.current.sessions.some(s => s.id === pending.sessionId);
    const target = { ...pending, sessionId: known ? pending.sessionId : sessionsRef.current.activeId };
    const settled = await followTurn(graphId, pending.turnId, {
      keepGoing: () => activeGraph.current === graphId && !turn.stopped && cancellation.isCurrent(turn),
      onProgress: t => setProgress(t.warning || `Thinking… (${t.elapsed ?? 0}s)`),
    });
    if (!settled) return;
    clearPendingTurn(turnStore, pending.turnId);
    setProgress('');
    const outcome = turnOutcome(settled);
    if (outcome.stopped) {
      appendTo(target.sessionId, { role: 'assistant', content: 'Stopped.', stopped: true });
    } else if (outcome.error) {
      appendTo(target.sessionId, { role: 'assistant', content: '', errors: [outcome.error] });
    } else {
      await settle(outcome.result || {}, target);
    }
  }, [graphId, turnStore, settle, appendTo, cancellation.isCurrent]);
  const driveRef = useRef(drive);
  driveRef.current = drive;

  const release = useCallback(turn => {
    if (cancellation.isCurrent(turn)) {
      busyRef.current = false;
      setBusy(false);
      setProgress('');
      cancellation.finish(turn);
    }
  }, [cancellation.isCurrent, cancellation.finish]);

  const submit = useCallback(async (text, { silent = false } = {}) => {
    if (!text || busyRef.current || !graphId) return;
    const turn = cancellation.begin();
    const before = getGraph();
    const history = conversationHistory(messagesRef.current);
    const pending = { turnId: turn.id, sessionId: sessions.activeId, before };

    setMessages((m) => [...m, { id: newId(), role: 'user', content: text, silent }]);
    busyRef.current = true;
    setBusy(true);
    // Recorded before the request goes out: if the chat closes while it is
    // in flight, the next mount still knows which turn to pick up.
    savePendingTurn(turnStore, pending);
    try {
      const res = await api.chatGraph(graphId, { message: text, history, graph: before, request_id: turn.id, background: true }, { signal: turn.controller.signal });
      if (turn.stopped || !cancellation.isCurrent(turn)) return;
      if (res?.turn_id) {
        await drive(turn, pending);
        return;
      }
      // A server without background turns answered inline.
      clearPendingTurn(turnStore, turn.id);
      if (activeGraph.current !== graphId) return;
      await settle(res, pending);
    } catch (err) {
      if (turn.stopped || !cancellation.isCurrent(turn)) return;
      clearPendingTurn(turnStore, turn.id);
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
      release(turn);
    }
  }, [graphId, getGraph, turnStore, drive, settle, release, setMessages, sessions.activeId, cancellation.begin, cancellation.isCurrent]);

  // Re-attach to a turn this workflow's chat started before it was closed
  // (another panel, another workflow, a reload). Keyed on the workflow alone;
  // everything else is reached through refs.
  useEffect(() => {
    if (!graphId) return undefined;
    let cancelled = false;
    const attach = pending => {
      if (cancelled || busyRef.current || !pending?.turnId) return;
      const turn = cancellation.begin(pending.turnId);
      busyRef.current = true;
      setBusy(true);
      setProgress('Thinking…');
      driveRef.current(turn, pending).finally(() => release(turn));
    };
    const stored = loadPendingTurn(chatHistoryKey(graphId));
    if (stored) attach(stored);
    else if (api.runningAssistantTurns) {
      // Browser storage unavailable or cleared: ask the server what is running.
      Promise.resolve(api.runningAssistantTurns(graphId, 'canvas')).then(r => {
        const running = r?.turns?.[0];
        if (running && !wasStopped(chatHistoryKey(graphId), running.turn_id)) {
          attach({ turnId: running.turn_id, sessionId: sessionsRef.current.activeId, before: getGraphRef.current() });
        }
      }).catch(() => {});
    }
    return () => { cancelled = true; };
  }, [graphId, release, cancellation.begin]);

  pollJobRef.current = pollJob;

  const send = useCallback(() => {
    const text = input.trim();
    if (!text) return;
    setInput('');
    submit(text);
  }, [input, submit]);

  const latestRunMessage = [...messages].reverse().find(m => m.runStarted);
  const trackedRun = latestRunMessage && !latestRunMessage.runHandled ? latestRunMessage.runStarted : null;
  useEffect(() => {
    if (!trackedRun || !api.getRun) return;
    let active = true, timer;
    setLiveRun(null); setRunError('');
    const poll = async () => {
      try {
        const result = await api.getRun(trackedRun);
        if (!active) return;
        setLiveRun(result); setRunError('');
        if (RUN_SETTLED.includes(result.status)) return;
      } catch (e) {
        if (!active) return;
        // The server no longer knows this run: it will never settle, so stop
        // asking and release the session controls.
        if (e.status === 404) { setLiveRun({ run_id: trackedRun, status: 'lost' }); return; }
        setRunError(e.message);
      }
      if (active) timer = setTimeout(poll, 1500);
    };
    poll();
    return () => { active = false; clearTimeout(timer); };
  }, [trackedRun, graphId]);

  // Completion is marked in history before requesting one diagnostic turn.
  // This survives reloads and prevents duplicate follow-ups.
  const observedOutcome = liveRun?.run_id === trackedRun ? liveRun : runOutcome;
  useEffect(() => {
    const runId = trackedRun || awaitingRun.current;
    if (!observedOutcome || !runId || observedOutcome.run_id !== runId || busy
        || !RUN_SETTLED.includes(observedOutcome.status)) return;
    awaitingRun.current = null;
    setMessages(rows => rows.map(m => m.runStarted === runId ? { ...m, runHandled: true, runStatus: observedOutcome.status } : m));
    // A lost run has nothing to read back; there is nothing to diagnose.
    if (observedOutcome.status === 'lost') return;
    submit(`The run you started (${runId}) finished with status "${observedOutcome.status}". Read this run, summarize its result and explain any failure. Suggest fixes; do not execute another run automatically.`, { silent: true });
  }, [observedOutcome, trackedRun, busy, submit]);

  const confirmRun = useCallback(
    async (id, proposal) => {
      const message = messagesRef.current.find((m) => m.id === id);
      if (!message?.pendingRun) return;
      setStartingRun(true);
      const patch = (fields) =>
        setMessages((m) => m.map((msg) => (msg.id === id ? { ...msg, ...fields } : msg)));
      try {
        // Saves the canvas first, so the run matches what is on screen.
        const chosen = proposal || message.pendingRun;
        const outcome = chosen.plan ? await onRunRequest(chosen.inputs || {}, chosen.start_at, chosen.session, chosen.plan_id, chosen.record) : await onRunRequest(chosen.inputs || {});
        // A run that was refused is not a run that started, whatever the
        // panel would rather say.
        if (!outcome?.ok || !outcome.runId) {
          const why = outcome?.error;
          patch({ errors: [why?.body?.detail || why?.message || String(why || 'The run did not start.')] });
          return outcome;
        }
        awaitingRun.current = outcome.runId;
        patch({ pendingRun: null, runStarted: outcome.runId, errors: [] });
        if (outcome.graphId && outcome.graphId !== graphId) {
          saveHistory(outcome.graphId, messagesRef.current.map(m => m.id === id ? { ...m, pendingRun: null, runStarted: outcome.runId, errors: [] } : m));
          window.dispatchEvent(new CustomEvent('workflow-chat-history', { detail: outcome.graphId }));
        }
        return outcome;
      } catch (err) {
        patch({ errors: [err?.body?.detail || err.message] });
        return { ok: false, error: err.message };
      } finally {
        setStartingRun(false);
      }
    },
    [onRunRequest, graphId, setMessages]
  );

  const dismissRun = useCallback((id) => {
    setMessages((m) =>
      m.map((msg) => (msg.id === id ? { ...msg, pendingRun: null, runDeclined: true } : msg))
    );
  }, [setMessages]);

  // Takes back a turn: the canvas as it was before it, Chat Agents included
  // (they are nodes of it).
  const undo = useCallback(
    (id) => {
      const message = messagesRef.current.find((m) => m.id === id);
      if (!message?.snapshot) return;
      setMessages((m) =>
        m.map((msg) => (msg.id === id ? { ...msg, snapshot: null, undone: true } : msg))
      );
      onApply(message.snapshot);
    },
    [onApply, setMessages]
  );

  // A generation that finished on a canvas the person had changed meanwhile
  // waits for them to choose.
  const replaceWithGenerated = useCallback((id) => {
    const message = messagesRef.current.find((m) => m.id === id);
    if (!message?.pendingGraph) return;
    const current = getGraphRef.current?.();
    onApply(message.pendingGraph);
    setMessages((m) => m.map((msg) => (msg.id === id ? { ...msg, pendingGraph: null, pendingSummary: null,
      summary: msg.pendingSummary || 'generated workflow', snapshot: current || null } : msg)));
  }, [onApply, setMessages]);
  const keepCanvas = useCallback((id) => {
    setMessages((m) => m.map((msg) => (msg.id === id ? { ...msg, pendingGraph: null, pendingSummary: null,
      notes: [...(msg.notes || []), 'Kept your canvas; the generated workflow was not applied.'] } : msg)));
  }, [setMessages]);

  async function stopChat() {
    const turnId = cancellation.current.current?.id;
    if (await cancellation.stop()) {
      clearPendingTurn(turnStore, turnId);
      rememberStopped(turnStore, turnId);
      busyRef.current = false;
      setBusy(false);
      setProgress('');
      setMessages(previous => [...previous.map(m => m.stage ? { ...m, stage: '', stopped: true } : m), { id: newId(), role: 'assistant', content: 'Stopped.', stopped: true }]);
    }
  }

  if (!graphId) {
    return <div className="chat-panel"><div className="muted small">Open a workflow to start.</div></div>;
  }

  return (
    <div className="chat-panel">
      <ChatSessions manager={sessions} busy={busy || startingRun || !!trackedRun} />
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
            {m.content && <CopyButton text={m.content} label={m.role === 'user' ? 'Copy your message' : 'Copy reply'} />}
            <ChatActivity activity={m.activity} />
            {m.summary && <div className="chat-summary">✓ {m.summary}</div>}
            {m.saved && <div className="chat-summary">💾 saved to disk</div>}
            {m.pendingRun && <WorkflowRunCard proposal={m.pendingRun} onRun={proposal => confirmRun(m.id, proposal)} onDismiss={() => dismissRun(m.id)} />}
            {m.runStarted && <div className="chat-summary">▶ run {m.runStarted} started{(m.runStatus || (liveRun?.run_id === m.runStarted && liveRun.status)) && <span> · {m.runStatus || liveRun.status}</span>}
              {liveRun?.run_id === m.runStarted && liveRun.status === 'running' && <button type="button" onClick={async () => { try { await api.cancelRun(m.runStarted); } catch (e) { setRunError(e.message); } }}>Stop run</button>}
            </div>}
            {m.runDeclined && <div className="chat-note">Run declined.</div>}
            {(m.tools || []).length > 0 && (
              <div className="chat-summary">🔧 {m.tools.map(name => {
                const check = [...(m.activity || [])].reverse().find(a => a.operation?.op === 'create_tool' && a.result?.name === name)?.result?.verification;
                const status = check?.status === 'verified' ? 'example tests passed' : check?.status === 'failed' ? 'verification failed' : 'not verified';
                return `${name} — created · ${status}`;
              }).join('; ')}</div>
            )}
            {(m.notes || []).map((n, j) => (
              <div key={j} className="chat-note">{n}</div>
            ))}
            {(m.errors || []).map((e, j) => (
              <div key={j} className="chat-error">{e}</div>
            ))}
            {m.stage && <div className="chat-thinking">{m.stage}</div>}
            {m.pendingGraph && (
              <div className="chat-confirm" role="group" aria-label="Apply generated workflow">
                <div className="chat-note">The canvas changed while generating. Replace it with the generated workflow?</div>
                <button type="button" onClick={() => replaceWithGenerated(m.id)}>Replace</button>
                <button type="button" onClick={() => keepCanvas(m.id)}>Keep mine</button>
              </div>
            )}
            {m.snapshot && (
              <button type="button" className="chat-undo" onClick={() => undo(m.id)}>
                Undo this change
              </button>
            )}
            {m.undone && <div className="chat-note">Change undone.</div>}
          </div>
        ))}

        {runError && <div role="alert" className="chat-error">{runError}</div>}
        {busy && <div className="chat-msg chat-assistant"><div className="chat-thinking" role="status">{progress || 'Thinking…'}</div></div>}
      </div>

      {cancellation.stopError && <div role="alert" className="chat-error">{cancellation.stopError}</div>}
      <ChatComposer value={input} onChange={setInput} onSend={send} busy={busy} onStop={stopChat} stopping={cancellation.stopping} disabled={disabled}
        placeholder={disabled ? 'Chat is unavailable right now' : 'Describe a workflow, a change, or ask it to run and debug…'} />
    </div>
  );
}
