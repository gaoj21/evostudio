import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useChatCancellation } from './useChatCancellation.js';
import ChatSessions from './ChatSessions.jsx';
import ChatActivity from './ChatActivity.jsx';
import ChatComposer from './ChatComposer.jsx';
import { useChatHistory, resultChatKey, newMessageId } from './chatSession.js';
import { followTurn, loadPendingTurn, savePendingTurn, clearPendingTurn, turnOutcome } from './assistantTurns.js';
import CopyButton from './CopyButton.jsx';
import { api } from '../../api.js';

export default function ResultChat({ graphId, run }) {
  const cancellation = useChatCancellation(graphId);
  const [messages, setMessages, sessions] = useChatHistory(resultChatKey(graphId));
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState('');
  const [error, setError] = useState('');
  const active = useRef(true);
  const busyRef = useRef(false);
  const store = resultChatKey(graphId);
  const { updateSession } = sessions;
  const sessionsRef = useRef(sessions);
  sessionsRef.current = sessions;
  const bottom = useRef(null);
  const context = sessions.context || { scope: 'current', run_id: run.run_id, batch_id: run.batch_id };
  useEffect(() => {
    if (!sessions.context) sessions.updateContext({ scope: 'current', run_id: run.run_id, batch_id: run.batch_id });
  }, [sessions.context, sessions.updateContext, run.run_id, run.batch_id]);
  useEffect(() => { setInput(''); setError(''); }, [sessions.activeId]);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, [graphId]);
  useEffect(() => { bottom.current?.scrollIntoView?.({ block: 'nearest' }); }, [messages, busy]);
  function selectScope(scope = context.scope) {
    sessions.updateContext({ scope: scope === 'batch' && !run.batch_id ? 'current' : scope, run_id: run.run_id, batch_id: run.batch_id });
    setError('');
  }
  // The id is made outside the state updater: React may run an updater more
  // than once, and a fresh id each time would remount the message.
  const appendTo = useCallback((sessionId, message) => {
    const row = { id: newMessageId(), ...message };
    updateSession(sessionId, m => [...m, row]);
  }, [updateSession]);
  // A question that could not be answered goes back into the composer, so
  // Retry resends it rather than leaving an unanswered message behind.
  // With nobody watching, the failure is kept in the conversation instead.
  const fail = useCallback((pending, message) => {
    if (!active.current) {
      appendTo(pending.sessionId, { role: 'assistant', content: `Could not answer: ${message}`, context: pending.context, failed: true });
      return;
    }
    updateSession(pending.sessionId, m => m.filter(x => x.id !== pending.questionId));
    setInput(value => value || pending.question || '');
    setError(message);
  }, [updateSession, appendTo]);
  const answer = useCallback((pending, response) => {
    appendTo(pending.sessionId, { role: 'assistant', content: response.reply, activity: response.activity, count: response.count, context: pending.context });
  }, [appendTo]);
  // The question runs on the server; leaving the results page only stops
  // watching it, and the next mount picks the answer up.
  const drive = useCallback(async (turn, pending) => {
    const known = sessionsRef.current.sessions.some(s => s.id === pending.sessionId);
    const target = known ? pending : { ...pending, sessionId: sessionsRef.current.activeId };
    const settled = await followTurn(graphId, pending.turnId, {
      keepGoing: () => active.current && !turn.stopped && cancellation.isCurrent(turn),
      onProgress: t => setProgress(t.warning || `Analyzing results… (${t.elapsed ?? 0}s)`),
    });
    if (!settled) return;
    clearPendingTurn(store, pending.turnId);
    const outcome = turnOutcome(settled);
    if (outcome.stopped) appendTo(target.sessionId, { role: 'assistant', content: 'Stopped.', context: target.context, stopped: true });
    else if (outcome.error) fail(target, outcome.error);
    else answer(target, outcome.result || {});
  }, [graphId, store, appendTo, fail, answer, cancellation.isCurrent]);
  const release = useCallback(turn => {
    if (cancellation.isCurrent(turn)) { busyRef.current = false; setBusy(false); setProgress(''); cancellation.finish(turn); }
  }, [cancellation.isCurrent, cancellation.finish]);
  const driveRef = useRef(drive);
  driveRef.current = drive;
  useEffect(() => {
    let cancelled = false;
    const attach = pending => {
      if (cancelled || busyRef.current || !pending?.turnId) return;
      const turn = cancellation.begin(pending.turnId);
      busyRef.current = true; setBusy(true);
      driveRef.current(turn, pending).finally(() => release(turn));
    };
    const stored = loadPendingTurn(resultChatKey(graphId));
    if (stored) attach(stored);
    else if (api.runningAssistantTurns) {
      Promise.resolve(api.runningAssistantTurns(graphId, 'results')).then(r => {
        const running = r?.turns?.[0];
        if (running) attach({ turnId: running.turn_id, sessionId: sessionsRef.current.activeId });
      }).catch(() => {});
    }
    return () => { cancelled = true; };
  }, [graphId, release, cancellation.begin]);
  async function send() {
    if (busyRef.current || !input.trim()) return;
    const question = input.trim();
    const turn = cancellation.begin();
    const pending = { turnId: turn.id, sessionId: sessions.activeId, questionId: newMessageId(), question, context };
    const history = messages.map(m => ({ role: m.role, content: m.context && JSON.stringify(m.context) !== JSON.stringify(context) ? `[Previous data context: ${JSON.stringify(m.context)}]\n${m.content}` : m.content }));
    setMessages(previous => [...previous, { id: pending.questionId, role: 'user', content: question, context }]);
    setInput('');
    busyRef.current = true; setBusy(true); setError('');
    savePendingTurn(store, pending);
    try {
      const response = await api.chatResults(graphId, { message: question, history, ...context, request_id: turn.id, background: true }, { signal: turn.controller.signal });
      if (turn.stopped || !cancellation.isCurrent(turn)) return;
      if (response?.turn_id) { await drive(turn, pending); return; }
      // A server without background turns answered inline.
      clearPendingTurn(store, turn.id);
      if (active.current) answer(pending, response);
    } catch (err) {
      if (turn.stopped || !cancellation.isCurrent(turn)) return;
      clearPendingTurn(store, turn.id);
      fail(pending, String(err.body?.detail || err.message));
    } finally { release(turn); }
  }
  async function stop() {
    const turnId = cancellation.current.current?.id;
    if (await cancellation.stop()) {
      clearPendingTurn(store, turnId);
      busyRef.current = false; setBusy(false); setProgress('');
      setMessages(previous => [...previous, { id: newMessageId(), role: 'assistant', content: 'Stopped.', context, stopped: true }]);
    }
  }
  const differentRecord = context.scope !== 'all' && (context.scope === 'batch' ? context.batch_id !== run.batch_id : context.run_id !== run.run_id);
  return <aside className="result-chat">
    <ChatSessions manager={sessions} busy={busy} />
    <div className="result-chat-context">
      <label>Data scope<select disabled={busy} value={context.scope} onChange={e => selectScope(e.target.value)}>
        <option value="current">Selected record</option><option value="batch" disabled={!run.batch_id && context.scope !== 'batch'}>Batch</option><option value="all">All task runs</option>
      </select></label>
      <small>{context.scope === 'all' ? 'All saved results for this task' : `${context.scope === 'batch' ? 'Batch' : 'Record'} · ${context.scope === 'batch' ? context.batch_id : context.run_id}`}</small>
      {differentRecord && <button disabled={busy} onClick={() => selectScope()}>Use selected record</button>}
    </div>
    <div className="result-chat-messages" aria-live="polite">
      {!messages.length && <div className="chat-welcome"><h3>Explore your results</h3><p>Ask a question, compare records, or calculate a metric.</p></div>}
      {messages.map((m, i) => <article key={m.id || i}><strong>{m.role === 'user' ? 'You' : 'Assistant'}</strong><div>{m.content}</div>
        {m.content && <CopyButton text={m.content} label={m.role === 'user' ? 'Copy your question' : 'Copy answer'} />}
        {m.count != null && <small>{m.count} run records</small>}
        <ChatActivity activity={m.activity} />
      </article>)}
      {busy && <p role="status">{progress || 'Analyzing results…'}</p>}<div ref={bottom}/>
    </div>
    {cancellation.stopError && <p role="alert">{cancellation.stopError}</p>}
    {error && <p role="alert">{error}</p>}
    <ChatComposer value={input} onChange={setInput} onSend={send} busy={busy} onStop={stop} stopping={cancellation.stopping} retry={!!error} label="Question about results" placeholder="Ask about these results…" />
  </aside>;
}
