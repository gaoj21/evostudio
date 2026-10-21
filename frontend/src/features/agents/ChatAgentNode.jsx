import React from 'react';
import { CHAT_HANDLES, handleStyle } from '../canvas/canvasHandles.js';
import { Handle } from '@xyflow/react';
// A Chat Agent on the canvas: a workflow node that does not run. Its handles
// connect it to memories; the inspector holds its settings and the chat.
export default function ChatAgentNode({ id, data, selected }) {
  return <div className={`memory-node chat-agent-node ${selected ? 'selected' : ''}`}>
    {!data.runMode && <button type="button" className="node-close nodrag" title="Delete Chat Agent" aria-label="Delete Chat Agent" onClick={e => { e.stopPropagation(); data.onDelete?.(id); }}>✕</button>}
    {CHAT_HANDLES.map(port => <Handle key={port.id} type={port.type} id={port.id} position={port.position} className="handle-memory" style={handleStyle(port)} />)}
    <div className="node-title">☏ {data.title || id}</div>
    <div className="node-desc">{data.description || 'Independent chat · not part of runs'}</div>
    <div className="node-desc">Click to chat or configure</div>
  </div>;
}
