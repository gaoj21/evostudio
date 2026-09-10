import { useCallback, useEffect, useState } from 'react';

let sequence = 0;
export const newMessageId = () => `m${Date.now().toString(36)}${++sequence}`;
export const chatHistoryKey = (graphId, page = 'canvas', scope = 'current', resource = '') => page === 'canvas'
  ? `evoagentx-studio:chat:${graphId}` : `result-chat:${graphId}:${scope}:${resource}`;
export const resultChatKey = graphId => `result-chat:${graphId}:page`;
const sessionsKey = key => `${key}:sessions`;
const normalize = messages => Array.isArray(messages) ? messages.filter(m => m && ['user', 'assistant'].includes(m.role)).map(m => m.id ? m : { ...m, id: newMessageId() }) : [];
function newSession(messages = []) {
  const now = Date.now();
  return { id: newMessageId(), title: messages.find(m => m.role === 'user' && !m.silent)?.content?.slice(0, 60) || 'New conversation', createdAt: now, updatedAt: now, messages };
}
function persist(key, data) {
  try {
    localStorage.setItem(sessionsKey(key), JSON.stringify(data));
    // Preserve compatibility with canvas rename/run callbacks and old clients.
    localStorage.setItem(key, JSON.stringify(data.sessions.find(s => s.id === data.activeId)?.messages || []));
  } catch { /* Conversation remains usable without browser storage. */ }
}
function loadStore(key) {
  try {
    const saved = JSON.parse(localStorage.getItem(sessionsKey(key)) || 'null');
    if (saved?.sessions?.length) {
      const sessions = saved.sessions.map(s => ({ ...s, messages: normalize(s.messages) }));
      return { ...saved, sessions, activeId: sessions.some(s => s.id === saved.activeId) ? saved.activeId : sessions[0].id };
    }
  } catch { /* Import legacy history below. */ }
  if (key.startsWith('result-chat:') && key.endsWith(':page')) {
    const prefix = key.slice(0, -4);
    const imported = [];
    try {
      const keys = Array.from({ length: localStorage.length }, (_, i) => localStorage.key(i));
      const bases = [...new Set(keys.filter(k => k?.startsWith(prefix) && k !== key && k !== sessionsKey(key)).map(k => k.endsWith(':sessions') ? k.slice(0, -9) : k))];
      for (const base of bases) {
        const suffix = base.slice(prefix.length);
        const separator = suffix.indexOf(':');
        const scope = suffix.slice(0, separator);
        const resource = suffix.slice(separator + 1);
        if (!['current', 'batch', 'all'].includes(scope)) continue;
        const old = loadStore(base);
        for (const session of old.sessions.filter(s => s.messages.length)) {
          const context = { scope, ...(scope === 'current' ? { run_id: resource } : scope === 'batch' ? { batch_id: resource } : {}) };
          imported.push({ ...session, id: newMessageId(), context, messages: session.messages.map(m => ({ ...m, context: m.context || context })) });
        }
      }
    } catch { /* Keep legacy data intact if migration is interrupted. */ }
    if (imported.length) {
      imported.sort((a, b) => b.updatedAt - a.updatedAt);
      const migrated = { version: 1, activeId: imported[0].id, sessions: imported };
      persist(key, migrated);
      return migrated;
    }
  }
  let messages = [];
  try { messages = normalize(JSON.parse(localStorage.getItem(key) || '[]')); } catch { /* Empty history. */ }
  const session = newSession(messages);
  const data = { version: 1, activeId: session.id, sessions: [session] };
  persist(key, data);
  return data;
}
function updateMessages(data, sessionId, messages) {
  return { ...data, sessions: data.sessions.map(s => s.id !== sessionId ? s : {
    ...s, messages, updatedAt: Date.now(), title: s.customTitle ? s.title : messages.find(m => m.role === 'user' && !m.silent)?.content?.slice(0, 60) || 'New conversation',
  }) };
}
export function loadChatHistory(key) {
  const data = loadStore(key);
  return data.sessions.find(s => s.id === data.activeId)?.messages || [];
}
export function saveChatHistory(key, messages) {
  const data = loadStore(key);
  persist(key, updateMessages(data, data.activeId, messages));
}
export function renameChatHistoryKey(oldKey, newKey) {
  try {
    for (const key of [oldKey, sessionsKey(oldKey)]) {
      const saved = localStorage.getItem(key);
      if (saved !== null) { localStorage.setItem(key.replace(oldKey, newKey), saved); localStorage.removeItem(key); }
    }
  } catch { /* Browser storage may be unavailable. */ }
}
export function useChatHistory(key) {
  const [state, setState] = useState(() => ({ key, data: loadStore(key) }));
  const data = state.key === key ? state.data : loadStore(key);
  const activeId = data.activeId;
  const messages = data.sessions.find(s => s.id === activeId)?.messages || [];
  useEffect(() => {
    setState(previous => previous.key === key ? previous : { key, data: loadStore(key) });
  }, [key]);
  useEffect(() => { if (state.key === key) persist(key, state.data); }, [key, state]);
  const change = useCallback(update => setState(previous => {
    const current = previous.key === key ? previous.data : loadStore(key);
    return { key, data: update(current) };
  }), [key]);
  // Bind updates to the originating session, including asynchronous replies.
  const setMessages = useCallback(update => change(current => {
    const session = current.sessions.find(s => s.id === activeId);
    if (!session) return current;
    return updateMessages(current, activeId, typeof update === 'function' ? update(session.messages) : update);
  }), [change, activeId]);
  const create = () => change(current => {
    const session = { ...newSession(), context: current.sessions.find(s => s.id === current.activeId)?.context };
    return { ...current, activeId: session.id, sessions: [session, ...current.sessions] };
  });
  const select = id => change(current => current.sessions.some(s => s.id === id) ? { ...current, activeId: id } : current);
  const rename = (id, title) => change(current => ({ ...current, sessions: current.sessions.map(s => s.id === id ? { ...s, title: title.trim().slice(0, 100), customTitle: true } : s) }));
  const remove = id => change(current => {
    const sessions = current.sessions.filter(s => s.id !== id);
    if (!sessions.length) sessions.push(newSession());
    return { ...current, sessions, activeId: current.activeId === id ? sessions[0].id : current.activeId };
  });
  const updateContext = useCallback(context => change(current => ({ ...current, sessions: current.sessions.map(s => s.id === activeId ? { ...s, context } : s) })), [change, activeId]);
  return [messages, setMessages, { context: data.sessions.find(s => s.id === activeId)?.context, updateContext, sessions: data.sessions, activeId, create, select, rename, remove }];
}
