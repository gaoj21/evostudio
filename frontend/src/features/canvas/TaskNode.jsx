import React from 'react';
import { Handle, Position } from '@xyflow/react';

export default function TaskNode({ id, data, selected }) {
  const status = data.runStatus || null;
  const cls = ['task-node'];
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
      <div className="node-title">{id}</div>
      <div className="node-desc">{data.description || <em>no description</em>}</div>
      {(data.tool_names || []).length > 0 && (
        <div className="node-tools" title={(data.tool_names || []).join(', ')}>
          🔧 {data.tool_names.length} tool{data.tool_names.length > 1 ? 's' : ''}
        </div>
      )}
      {data.batchBadge && <div className="node-tools batch-badge">{data.batchBadge}</div>}
      {status && <div className={`node-status status-${status}`}>{status}</div>}
      <Handle type="source" position={Position.Right} />
      <Handle type="target" id="t-in" position={Position.Top} className="handle-memory" />
      <Handle type="source" id="b-out" position={Position.Bottom} className="handle-memory" style={{ left: '38%' }} />
      <Handle type="target" id="b-in" position={Position.Bottom} className="handle-memory" style={{ left: '62%' }} />
    </div>
  );
}
