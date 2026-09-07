import React, { useEffect, useState } from 'react';
import { api } from '../api.js';
import { toolDefaults } from './ToolsPanel.jsx';

export default function Palette({ templates, sources, graphTemplates, onAdd, onLoadTemplate, disabled }) {
  const [toolCatalog, setToolCatalog] = useState([]);

  useEffect(() => {
    api.listTools().then((r) => setToolCatalog(r.tools || [])).catch(() => {});
  }, []);

  const onDragStart = (e, tpl) => {
    e.dataTransfer.setData('application/evoagentx-template', JSON.stringify(tpl));
    e.dataTransfer.effectAllowed = 'move';
  };
  const item = (tpl) => (
    <div
      key={tpl.type}
      className={`palette-item${disabled ? ' disabled' : ''}`}
      draggable={!disabled}
      onDragStart={(e) => onDragStart(e, tpl)}
      onClick={() => !disabled && onAdd(tpl)}
      title={tpl.description}
    >
      <div className="palette-label">{tpl.label}</div>
      <div className="palette-desc">{tpl.description}</div>
    </div>
  );

  // one draggable palette entry per sub-tool (built-in or custom); custom
  // tools are created/managed in the left "Tools" tab
  const toolItems = toolCatalog
    .filter((t) => t.available)
    .flatMap((t) => (t.tools || []).map((sub) => ({ toolkit: t.name, ...sub })));
  const toolNode = (sub) => (
    <div
      key={`${sub.toolkit}:${sub.name}`}
      className={`palette-item tool-item${disabled ? ' disabled' : ''}`}
      draggable={!disabled}
      onDragStart={(e) => onDragStart(e, { type: sub.name, label: sub.name, defaults: toolDefaults(sub) })}
      onClick={() => !disabled && onAdd({ type: sub.name, label: sub.name, defaults: toolDefaults(sub) })}
      title={`${sub.description} (${sub.toolkit})`}
    >
      <div className="palette-label">⚙ {sub.name}</div>
      <div className="palette-desc">{sub.description}</div>
    </div>
  );

  return (
    <aside className="palette">
      <h3>Nodes</h3>
      <button
        type="button"
        className="palette-custom-add"
        disabled={disabled}
        onClick={() =>
          onAdd({
            type: 'custom',
            label: 'Custom node',
            defaults: {
              description: '',
              inputs: [{ name: 'input', type: 'str', description: '', required: true }],
              outputs: [{ name: 'output', type: 'str', description: '', required: true }],
              prompt: '',
              system_prompt: '',
              parse_mode: 'str',
            },
          })
        }
      >
        + Custom node
      </button>
      <p className="palette-hint">Drag or click to add</p>
      {templates.map(item)}
      {(sources || []).length > 0 && (
        <>
          <h3>Input Sources</h3>
          <p className="palette-hint">Feed data into the workflow</p>
          {sources.map(item)}
        </>
      )}
      {(graphTemplates || []).length > 0 && (
        <>
          <h3>Templates</h3>
          <p className="palette-hint">Load a ready-made graph</p>
          {graphTemplates.map((t) => (
            <div
              key={t.id}
              className={`palette-item${disabled ? ' disabled' : ''}`}
              onClick={() => !disabled && onLoadTemplate(t.id)}
              title={t.description}
            >
              <div className="palette-label">{t.name}</div>
              <div className="palette-desc">{t.description}</div>
            </div>
          ))}
        </>
      )}
      <h3>Tools</h3>
      <p className="palette-hint">Drag onto the canvas as a tool node; manage custom tools in the "Tools" tab</p>
      {toolItems.map(toolNode)}
    </aside>
  );
}
