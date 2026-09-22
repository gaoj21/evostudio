import React from 'react';
import { CHAT_HANDLES, handleStyle } from '../canvas/canvasHandles.js';
import { Handle } from '@xyflow/react';
export default function ChatAgentNode({ id, data, selected }) {
  return <div className={`memory-node chat-agent-node ${selected ? 'selected' : ''}`}>
    {!data.runMode && <button type="button" className="node-close nodrag" title="Remove Chat Agent from canvas" aria-label="Remove Chat Agent from canvas" onClick={e => { e.stopPropagation(); data.onDelete?.(id); }}>✕</button>}
    {CHAT_HANDLES.map(port => <Handle key={port.id} type={port.type} id={port.id} position={port.position} className="handle-memory" style={handleStyle(port)} />)}
    <div className="node-title">☏ {data.title}</div>
    <div className="node-desc">Independent chat · Deep Agents</div>
    <div className="node-desc">Click to chat or configure</div>
  </div>;
}
