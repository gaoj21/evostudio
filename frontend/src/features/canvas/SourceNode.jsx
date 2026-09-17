import React from 'react';
import { Handle, Position } from '@xyflow/react';

export default function SourceNode({ id, data, selected }) {
  const status = data.runStatus || null;
  const cls = ['task-node', 'source-node'];
  if (selected) cls.push('selected');
  if (status) cls.push(`run-${status}`);
  const cfg = data.source || {};
  const preview = Object.entries(cfg)
    .filter(([k]) => !['type','code','input_schema','output_schema','preview_snapshot'].includes(k))
    .slice(0, 3)
    .map(([k, v]) => `${k}=${String(v).slice(0, 24)}`)
    .join(' · ');
  return (
    <div className={cls.join(' ')}>
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
      <div className="node-title">
        ⇥ {id}
        {data.watching && <span className="watch-mark" title="watching (scheduled)"> ⏱</span>}
      </div>
      <div className="node-desc">
        {cfg.type || 'source'}
        {preview ? ` · ${preview}` : ''}
      </div>
      {cfg.type === 'dataloader' && <div className="node-tools">
        <div>{cfg.output_schema?.length ? 'Interface ready' : 'Draft · preview to define outputs'}</div>
        {!!cfg.input_schema?.inputs?.length && <div>Inputs: {cfg.input_schema.inputs.map(f=>f.name).join(', ')}</div>}
        {!!data.outputs?.length && <div>Outputs: {data.outputs.map(f=>`${f.name}: ${f.type}`).join(', ')}</div>}
      </div>}
      {data.batchBadge && <div className="node-tools batch-badge">{data.batchBadge}</div>}
      {status && <div className={`node-status status-${status}`}>{status}</div>}
      <Handle type="source" position={Position.Right} />
      <Handle type="target" id="t-in" position={Position.Top} className="handle-memory" />
      <Handle type="source" id="b-out" position={Position.Bottom} className="handle-memory" style={{ left: '38%' }} />
      <Handle type="target" id="b-in" position={Position.Bottom} className="handle-memory" style={{ left: '62%' }} />
    </div>
  );
}
