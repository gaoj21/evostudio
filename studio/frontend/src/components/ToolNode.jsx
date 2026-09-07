import React from 'react';
import { Handle, Position } from '@xyflow/react';

export default function ToolNode({ id, data, selected }) {
  const status = data.runStatus || null;
  const cls = ['task-node', 'tool-node'];
  if (selected) cls.push('selected');
  if (status) cls.push(`run-${status}`);
  return (
    <div className={cls.join(' ')}>
      <Handle type="target" position={Position.Left} />
      {!data.runMode && (
        <button
          className="node-close nodrag"
          title="Delete node"
          onClick={(e) => {
            e.stopPropagation();
            data.onDelete && data.onDelete(id);
          }}
        >
          ✕
        </button>
      )}
      <div className="node-title">⚙ {id}</div>
      <div className="node-desc">{data.tool || 'tool'}</div>
      {data.batchBadge && <div className="node-tools batch-badge">{data.batchBadge}</div>}
      {status && <div className={`node-status status-${status}`}>{status}</div>}
      <Handle type="source" position={Position.Right} />
    </div>
  );
}
