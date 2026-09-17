import React, { useEffect, useState } from 'react';
import ResourceActions from '../../components/ResourceActions.jsx';
import { api } from '../../api.js';
import { toolDefaults } from './ToolsPanel.jsx';

function PaletteSection({ title, count, initiallyOpen = false, searching, children }) {
  const [open, setOpen] = useState(initiallyOpen);
  if (!count) return null;
  const visible = searching || open;
  return (
    <section className="palette-section">
      <button
        type="button"
        className="palette-section-toggle"
        aria-expanded={visible}
        onClick={() => setOpen((value) => !value)}
      >
        <span>{title}</span>
        <span className="palette-section-meta"><span className="palette-count">{count}</span><span aria-hidden="true">{visible ? '⌃' : '⌄'}</span></span>
      </button>
      {visible && <div className="palette-section-body">{children}</div>}
    </section>
  );
}

export default function Palette({ templates, sources, graphTemplates, onAdd, onLoadTemplate, disabled }) {
  const [toolCatalog, setToolCatalog] = useState([]);
  const [query, setQuery] = useState('');

  useEffect(() => {
    api.listTools().then((r) => setToolCatalog(r.tools || [])).catch(() => {});
  }, []);

  const onDragStart = (e, tpl) => {
    e.dataTransfer.setData('application/evoagentx-template', JSON.stringify(tpl));
    e.dataTransfer.effectAllowed = 'move';
  };
  const item = (tpl) => (
    <ResourceActions key={tpl.type} name={tpl.label} items={[{ label: 'Add to canvas', disabled, action: () => onAdd(tpl) }, { label: 'Built-in component · read only', disabled: true }]}><button
      type="button"
      disabled={disabled}
      key={tpl.type}
      className={`palette-item${disabled ? ' disabled' : ''}`}
      draggable={!disabled}
      onDragStart={(e) => onDragStart(e, tpl)}
      onClick={() => !disabled && onAdd(tpl)}
      title={tpl.description}
    >
      <div className="palette-label"><span>{tpl.label}</span><span className="palette-add" aria-hidden="true">+</span></div>
      <div className="palette-desc">{tpl.description}</div>
    </button></ResourceActions>
  );

  // one draggable palette entry per sub-tool (built-in or custom); custom
  // tools are created/managed in the unified Library and Custom tabs
  const toolItems = toolCatalog
    .filter((t) => t.available)
    .flatMap((t) => (t.tools || []).map((sub) => ({ toolkit: t.name, ...sub })));

  const matches = (entry) => {
    const haystack = [entry.label, entry.name, entry.type, entry.description, entry.toolkit]
      .filter(Boolean)
      .join(' ')
      .toLowerCase();
    return haystack.includes(query.trim().toLowerCase());
  };
  const domain = (templates || []).filter((tpl) => /^CR\b/i.test(tpl.label || ''));
  const core = [...(templates || []).filter((tpl) => !domain.includes(tpl)),
    {type:'evaluate',label:'Evaluator',description:'Score intermediate or final outputs; use the same objective in Evolve.',defaults:{kind:'evaluator',evaluator:{type:'python',timing:'batch'},inputs:[{name:'prediction',type:'any',required:false},{name:'expected',type:'any',required:false}],outputs:[]}}];
  const filtered = {
    core: core.filter(matches),
    domain: domain.filter(matches),
    sources: (sources || []).filter(matches),
    graphTemplates: (graphTemplates || []).filter(matches),
    tools: toolItems.filter(matches),
  };
  const searching = query.trim().length > 0;
  const resultCount = Object.values(filtered).reduce((total, entries) => total + entries.length, 0);
  const toolNode = (sub) => (
    <ResourceActions key={`${sub.toolkit}:${sub.name}`} name={sub.name} items={[{ label: 'Add to canvas', disabled, action: () => onAdd({ type: sub.name, label: sub.name, defaults: toolDefaults(sub) }) }, { label: 'Manage custom tools in Custom', disabled: true }]}><button
      type="button"
      disabled={disabled}
      key={`${sub.toolkit}:${sub.name}`}
      className={`palette-item tool-item${disabled ? ' disabled' : ''}`}
      draggable={!disabled}
      onDragStart={(e) => onDragStart(e, { type: sub.name, label: sub.name, defaults: toolDefaults(sub) })}
      onClick={() => !disabled && onAdd({ type: sub.name, label: sub.name, defaults: toolDefaults(sub) })}
      title={`${sub.description} (${sub.toolkit})`}
    >
      <div className="palette-label"><span>{sub.name}</span><span className="palette-add" aria-hidden="true">+</span></div><div className="palette-toolkit">{sub.toolkit}</div>
      <div className="palette-desc">{sub.description}</div>
    </button></ResourceActions>
  );

  return (
    <aside className="palette">
      <div className="palette-title-row">
        <h3>Components</h3>
        <span className="muted small">Click or drag</span>
      </div>
      <div className="palette-search-wrap"><span aria-hidden="true">⌕</span><input
        className="palette-search"
        type="search"
        aria-label="Search node library"
        placeholder="Search nodes and tools…"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
      /></div>
      <PaletteSection title="Core nodes" count={filtered.core.length} initiallyOpen searching={searching}>
        {filtered.core.map(item)}
      </PaletteSection>
      <PaletteSection title="Input sources" count={filtered.sources.length} searching={searching}>
        {filtered.sources.map(item)}
      </PaletteSection>
      <PaletteSection title="Credit risk" count={filtered.domain.length} searching={searching}>
        {filtered.domain.map(item)}
      </PaletteSection>
      <PaletteSection title="Workflow templates" count={filtered.graphTemplates.length} searching={searching}>
        {filtered.graphTemplates.map((template) => (
            <ResourceActions key={template.id} name={template.name} items={[{ label: 'Load workflow template', disabled, action: () => onLoadTemplate(template.id) }, { label: 'Built-in template · read only', disabled: true }]}><button
              type="button"
              disabled={disabled}
              key={template.id}
              className={`palette-item${disabled ? ' disabled' : ''}`}
              onClick={() => !disabled && onLoadTemplate(template.id)}
              title={template.description}
            >
              <div className="palette-label">{template.name}</div>
              <div className="palette-desc">{template.description}</div>
            </button></ResourceActions>
        ))}
      </PaletteSection>
      <PaletteSection title="Tools" count={filtered.tools.length} searching={searching}>
        {filtered.tools.map(toolNode)}
      </PaletteSection>
      {searching && resultCount === 0 && (
        <p className="palette-empty">No nodes or tools match “{query.trim()}”.</p>
      )}
    </aside>
  );
}
