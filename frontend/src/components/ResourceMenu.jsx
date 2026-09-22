import React, { useEffect, useLayoutEffect, useRef, useState } from 'react';

export default function ResourceMenu({ menu, items, onClose }) {
  const ref = useRef(null);
  const [pos, setPos] = useState({ left: menu.x, top: menu.y });
  useLayoutEffect(() => {
    const box = ref.current.getBoundingClientRect();
    setPos({ left: Math.max(8, Math.min(menu.x, window.innerWidth - box.width - 8)), top: Math.max(8, Math.min(menu.y, window.innerHeight - box.height - 8)) });
    ref.current.querySelector('button:not(:disabled)')?.focus();
  }, [menu.x, menu.y]);
  useEffect(() => {
    const away = e => { if (!ref.current?.contains(e.target)) onClose(); };
    const key = e => { if (e.key === 'Escape') { onClose(); menu.trigger?.focus(); } };
    document.addEventListener('pointerdown', away);
    document.addEventListener('keydown', key);
    return () => { document.removeEventListener('pointerdown', away); document.removeEventListener('keydown', key); };
  }, [onClose, menu.trigger]);
  return <div ref={ref} role="menu" aria-label={`${menu.task.name} actions`} className="ctx-menu" style={pos} onKeyDown={e => {
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) return;
    e.preventDefault();
    const buttons = [...ref.current.querySelectorAll('button:not(:disabled)')];
    if (!buttons.length) return;
    const index = buttons.indexOf(document.activeElement);
    buttons[e.key === 'Home' ? 0 : e.key === 'End' ? buttons.length - 1 : (index + (e.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length]?.focus();
  }}><div className="ctx-path">{menu.task.name}</div>{items.map(item => <button type="button" role="menuitem" key={item.label} disabled={item.disabled} className={`ctx-item${item.danger ? ' ctx-danger' : ''}`} onClick={() => { onClose(); item.action(); }}>{item.label}</button>)}</div>;
}
