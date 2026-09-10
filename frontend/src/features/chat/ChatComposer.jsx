import React from 'react';

export default function ChatComposer({ value, onChange, onSend, busy, onStop, stopping = false, disabled, placeholder, label = 'Chat message', retry = false }) {
  function submit(event) {
    event?.preventDefault();
    if (!busy && !disabled && value.trim()) onSend();
  }
  return <form className="chat-input" onSubmit={submit}>
    <textarea rows={3} aria-label={label} placeholder={placeholder} value={value} disabled={busy || disabled}
      onChange={event => onChange(event.target.value)} onKeyDown={event => {
        if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) submit(event);
      }} />
    {busy && onStop ? <button type="button" className="chat-stop" disabled={stopping} onClick={onStop}>{stopping ? 'Stopping…' : '■ Stop'}</button> : <button type="submit" className="primary" disabled={busy || disabled || !value.trim()}>{busy ? 'Working…' : retry ? 'Retry' : 'Send'}</button>}
  </form>;
}
