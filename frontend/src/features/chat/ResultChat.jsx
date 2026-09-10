import React, { useEffect, useRef, useState } from 'react';
import { useChatCancellation } from './useChatCancellation.js';
import ChatSessions from './ChatSessions.jsx';
import ChatActivity from './ChatActivity.jsx';
import ChatComposer from './ChatComposer.jsx';
import { useChatHistory, resultChatKey } from './chatSession.js';
import { api } from '../../api.js';

export default function ResultChat({ graphId, run }) {
  const cancellation = useChatCancellation(graphId);
  const [messages, setMessages, sessions] = useChatHistory(resultChatKey(graphId));
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const active = useRef(true);
  const bottom = useRef(null);
  const context = sessions.context || { scope: 'current', run_id: run.run_id, batch_id: run.batch_id };
  useEffect(() => {
    if (!sessions.context) sessions.updateContext({ scope: 'current', run_id: run.run_id, batch_id: run.batch_id });
  }, [sessions.context, sessions.updateContext, run.run_id, run.batch_id]);
  useEffect(() => { setInput(''); setError(''); }, [sessions.activeId]);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, [graphId]);
  useEffect(() => { bottom.current?.scrollIntoView?.({ block: 'nearest' }); }, [messages, busy]);
  function useSelected(scope = context.scope) {
    sessions.updateContext({ scope: scope === 'batch' && !run.batch_id ? 'current' : scope, run_id: run.run_id, batch_id: run.batch_id });
    setError('');
  }
  async function send() {
    if (busy || !input.trim()) return;
    const question = input.trim();
    const turn = cancellation.begin();
    setBusy(true); setError('');
    try {
      const history = messages.map(m => ({ role: m.role, content: m.context && JSON.stringify(m.context) !== JSON.stringify(context) ? `[Previous data context: ${JSON.stringify(m.context)}]\n${m.content}` : m.content }));
      const response = await api.chatResults(graphId, { message: question, history, ...context, request_id: turn.id }, { signal: turn.controller.signal });
      if (!active.current || turn.stopped || !cancellation.isCurrent(turn)) return;
      setMessages(previous => [...previous, { role: 'user', content: question, context }, { role: 'assistant', content: response.reply, activity: response.activity, count: response.count, context }]);
      setInput('');
    } catch (err) { if (active.current && !turn.stopped) setError(String(err.body?.detail || err.message)); }
    finally { if (active.current && cancellation.isCurrent(turn)) { setBusy(false); cancellation.finish(turn); } }
  }
  async function stop() {
    if (await cancellation.stop()) {
      setBusy(false);
      setMessages(previous => [...previous, { role: 'user', content: input.trim(), context }, { role: 'assistant', content: 'Stopped.', context, stopped: true }]);
      setInput('');
    }
  }
  const differentRecord = context.scope !== 'all' && (context.scope === 'batch' ? context.batch_id !== run.batch_id : context.run_id !== run.run_id);
  return <aside className="result-chat">
    <ChatSessions manager={sessions} busy={busy} />
    <div className="result-chat-context">
      <label>Data scope<select disabled={busy} value={context.scope} onChange={e => useSelected(e.target.value)}>
        <option value="current">Selected record</option><option value="batch" disabled={!run.batch_id && context.scope !== 'batch'}>Batch</option><option value="all">All task runs</option>
      </select></label>
      <small>{context.scope === 'all' ? 'All saved results for this task' : `${context.scope === 'batch' ? 'Batch' : 'Record'} · ${context.scope === 'batch' ? context.batch_id : context.run_id}`}</small>
      {differentRecord && <button disabled={busy} onClick={() => useSelected()}>Use selected record</button>}
    </div>
    <div className="result-chat-messages" aria-live="polite">
      {!messages.length && <div className="chat-welcome"><h3>Explore your results</h3><p>Ask a question, compare records, or calculate a metric.</p></div>}
      {messages.map((m, i) => <article key={m.id || i}><strong>{m.role === 'user' ? 'You' : 'Assistant'}</strong><div>{m.content}</div>
        {m.count != null && <small>{m.count} run records</small>}
        <ChatActivity activity={m.activity} />
      </article>)}
      {busy && <p role="status">Analyzing results…</p>}<div ref={bottom}/>
    </div>
    {cancellation.stopError && <p role="alert">{cancellation.stopError}</p>}
    {error && <p role="alert">{error}</p>}
    <ChatComposer value={input} onChange={setInput} onSend={send} busy={busy} onStop={stop} stopping={cancellation.stopping} retry={!!error} label="Question about results" placeholder="Ask about these results…" />
  </aside>;
}
