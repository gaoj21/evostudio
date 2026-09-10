import React, { useEffect, useRef, useState } from 'react';
import ResourceActions from '../../components/ResourceActions.jsx';

export default function ChatSessions({ manager, busy = false }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [editing, setEditing] = useState(null);
  const [title, setTitle] = useState('');
  const [deleting, setDeleting] = useState(null);
  const trigger = useRef(null);
  const drawer = useRef(null);
  useEffect(() => {
    if (!open) return;
    drawer.current?.querySelector('input')?.focus();
    return () => { trigger.current?.focus(); };
  }, [open]);
  function create() { manager.create(); setOpen(false); setEditing(null); setDeleting(null); }
  const current = manager.sessions.find(s => s.id === manager.activeId);
  return <div className="chat-session-toolbar">
    <button ref={trigger} className="chat-history-trigger" aria-label={`History (${manager.sessions.length})`} aria-haspopup="dialog" onClick={() => setOpen(true)}>☰</button>
    <span title={current?.title}>{current?.title || 'New conversation'}</span>
    <button disabled={busy} onClick={create} aria-label="New session" title="New session">＋ New session</button>
    {open && <div className="chat-history-backdrop" onClick={() => setOpen(false)}>
      <aside ref={drawer} className="chat-history-drawer" role="dialog" aria-modal="false" aria-label="Conversation history" onKeyDown={event => { if (event.key === 'Escape') setOpen(false); event.stopPropagation(); }} onClick={e => e.stopPropagation()}>
        <header><div><small>CHAT</small><h2>Conversations</h2></div><button aria-label="Close history" onClick={() => setOpen(false)}>✕</button></header>
        <input aria-label="Search conversations" placeholder="Search conversations…" value={query} onChange={e => setQuery(e.target.value)} />
        <button className="chat-new-session" disabled={busy} onClick={create}>＋ New session</button>
        {busy && <p className="muted small">Finish the current operation to switch conversations.</p>}
        <div className="chat-history-items">
          {[...manager.sessions].sort((a,b) => b.updatedAt-a.updatedAt).filter(s => s.title.toLowerCase().includes(query.toLowerCase())).map(session => <div key={session.id} className={`chat-history-item ${session.id === manager.activeId ? 'active' : ''}`}>
            <ResourceActions name={session.title} items={[
              {label:'Rename',disabled:busy,action:()=>{setEditing(session.id);setTitle(session.title);setDeleting(null);}},
              {label:'Delete',danger:true,disabled:busy,action:()=>{setDeleting(session.id);setEditing(null);}},
            ]}>
              <button disabled={busy} aria-label={`Open session ${session.title}`} aria-current={session.id === manager.activeId ? 'true' : undefined} onClick={() => { manager.select(session.id); setOpen(false); setEditing(null); setDeleting(null); }}>
                <strong>{session.title}</strong><small>{new Date(session.updatedAt).toLocaleDateString()} · {session.messages.length} messages</small>
              </button>
            </ResourceActions>
            {editing === session.id && <form onSubmit={e => { e.preventDefault(); if (title.trim()) { manager.rename(session.id,title); setEditing(null); } }}><input aria-label="Session name" value={title} onChange={e => setTitle(e.target.value)} maxLength={100}/><button disabled={busy || !title.trim()}>Save name</button><button type="button" onClick={() => setEditing(null)}>Cancel</button></form>}
            {deleting === session.id && <div className="chat-history-confirm"><p>Delete this conversation?</p><button disabled={busy} onClick={() => { manager.remove(session.id); setDeleting(null); }}>Confirm delete</button><button onClick={() => setDeleting(null)}>Cancel</button></div>}
          </div>)}
          {!manager.sessions.some(s => s.title.toLowerCase().includes(query.toLowerCase())) && <p>No conversations found.</p>}
        </div>
        <footer>Saved in this browser</footer>
      </aside>
    </div>}
  </div>;
}
