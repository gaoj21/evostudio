import React, { useState } from 'react';
import { createPortal } from 'react-dom';
import ResourceMenu from './ResourceMenu.jsx';

/** One menu shared by pointer, keyboard and touch entry points. */
export default function ResourceActions({ name, items, children, className = '' }) {
  const [menu, setMenu] = useState(null);
  function open(e) {
    e.preventDefault(); e.stopPropagation();
    const box = e.currentTarget.getBoundingClientRect();
    setMenu({ task: { name }, trigger: e.currentTarget,
      x: e.type === 'contextmenu' ? e.clientX : box.left,
      y: e.type === 'contextmenu' ? e.clientY : box.bottom });
  }
  return <div className={`resource-actions ${className}`} onContextMenu={open}>
    <div className="resource-actions-content">{children}</div>
    <button type="button" className="resource-actions-trigger" aria-label={`Actions for ${name}`} aria-haspopup="menu" aria-expanded={!!menu} onClick={open}>⋯</button>
    {menu && createPortal(<div onClick={e => e.stopPropagation()} onContextMenu={e => e.stopPropagation()}><ResourceMenu menu={menu} items={items} onClose={() => setMenu(null)} /></div>, document.body)}
  </div>;
}
