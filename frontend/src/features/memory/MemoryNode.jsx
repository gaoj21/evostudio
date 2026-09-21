import React from 'react';
import { MEMORY_HANDLES, handleStyle } from '../canvas/canvasHandles.js';
import { Handle } from '@xyflow/react';

/**
 * A node's memory store, drawn on the canvas.
 *
 * Memory used to live behind a tab: which node stored, what it kept, and who
 * read it back were settings you had to open each node to discover. Here the
 * store is a thing with edges — a write into it, reads out of it — so the
 * canvas answers "where is this remembered, and who looks it up" at a glance.
 * Derived from the owner's settings; selecting it opens them.
 */
export default function MemoryNode({ id, data, selected }) {
  const cls = ['memory-node', data.kind === 'table' ? 'memory-table' : 'memory-corpus'];
  if (selected) cls.push('selected');
  const keeps = data.keeps.length ? data.keeps.join(', ') : 'nothing selected';
  return (
    <div className={cls.join(' ')} title={`${data.title}. Drag to position; click to manage connections.`}>
      {!data.runMode && <button type="button" className="node-close nodrag" title="Remove memory from canvas" aria-label="Remove memory from canvas" onClick={e => { e.stopPropagation(); data.onDelete?.(id); }}>✕</button>}
      {MEMORY_HANDLES.map(port => <Handle key={port.id} type={port.type} id={port.id} position={port.position} className="handle-memory" title={port.type === 'source' ? 'Read from memory' : 'Write into memory'} style={handleStyle(port)} />)}
      <div className="node-title">
        {data.kind === 'table' ? '▤' : '≋'} {data.title}
      </div>
      <div className="node-desc">
        {data.kind === 'table'
          ? <>records{data.match ? <> matching <b>{data.match}</b></> : null}{data.at ? <>, dated by <b>{data.at}</b></> : null}</>
          : data.kind === 'mem0' ? 'shared project space' : 'searchable corpus of past runs'}
      </div>
      <div className="node-desc">{data.readers?.length || 0} readers · {data.writers?.length || 0} writers</div>
      <div className="node-desc">{data.kind === 'mem0' ? 'Mem0 · shared space' : data.write_enabled === false ? "Writing disabled · existing history retained" : `keeps ${keeps}`}</div>
      {data.wrote > 0 && <div className="node-tools batch-badge">wrote {data.wrote}</div>}
    </div>
  );
}
