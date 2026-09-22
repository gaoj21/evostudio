import React, { useEffect, useState } from 'react';

// Copies one message's text. Selecting the text by hand works too; this is the
// one-click path, with a fallback where the async clipboard API is refused.
export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    try {
      const area = document.createElement('textarea');
      area.value = text;
      area.setAttribute('readonly', '');
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      const copied = document.execCommand?.('copy');
      area.remove();
      return !!copied;
    } catch { return false; }
  }
}

export default function CopyButton({ text, label = 'Copy message' }) {
  const [state, setState] = useState('');
  useEffect(() => {
    if (!state) return undefined;
    const timer = setTimeout(() => setState(''), 1500);
    return () => clearTimeout(timer);
  }, [state]);
  return <button type="button" className="chat-copy" aria-label={label} title={label}
    onClick={async () => setState(await copyText(text) ? 'Copied' : 'Copy failed')}>
    {state || 'Copy'}
  </button>;
}
