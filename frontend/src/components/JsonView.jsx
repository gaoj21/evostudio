import React, { useMemo, useState } from 'react';
import { unfold, pretty } from '../jsonView.js';

// Past this many items an array is cut, with a click to see the rest: a
// news batch is hundreds of records and the drawer is not the place to
// scroll them.
const ARRAY_CUT = 20;

/**
 * A value as a tree: keys, colours, and a fold on every object and array.
 *
 * `Raw` shows the same value as indented text for when a tree is the wrong
 * tool (diffing by eye, pasting somewhere). `Copy` copies that text — the
 * unfolded one, so what you paste is the JSON, not the escaped string.
 */
export default function JsonView({ value, className = '', label, startOpen = true }) {
  const [mode, setMode] = useState('tree');
  const [copied, setCopied] = useState(false);
  const data = useMemo(() => unfold(value), [value]);
  const text = useMemo(() => pretty(value), [value]);
  const structured = data != null && typeof data === 'object';

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch {
      /* clipboard refused: nothing to do */
    }
  };

  if (!structured) {
    return <pre className={`json-view ${className}`}>{text}</pre>;
  }
  return (
    <div className={`json-view json-tree-wrap ${className}`} data-testid="json-view">
      <div className="json-toolbar">
        {label && <span className="muted small">{label}</span>}
        <span className="json-toolbar-spacer" />
        <button
          type="button"
          className={`link small ${mode === 'tree' ? 'active' : ''}`}
          onClick={() => setMode('tree')}
          aria-pressed={mode === 'tree'}
        >
          Tree
        </button>
        <button
          type="button"
          className={`link small ${mode === 'raw' ? 'active' : ''}`}
          onClick={() => setMode('raw')}
          aria-pressed={mode === 'raw'}
        >
          Raw
        </button>
        <button type="button" className="link small" onClick={copy}>
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      {mode === 'raw' ? (
        <pre className="json-raw">{text}</pre>
      ) : (
        <div className="json-tree" role="tree">
          <Node value={data} depth={0} last open0={startOpen} />
        </div>
      )}
    </div>
  );
}

function Node({ name, value, depth, last, open0 = true }) {
  const isArray = Array.isArray(value);
  const isObject = value != null && typeof value === 'object' && !isArray;
  const [open, setOpen] = useState(open0);
  const [showAll, setShowAll] = useState(false);

  const key = name !== undefined && (
    <>
      <span className="json-key">{name}</span>
      <span className="json-punct">: </span>
    </>
  );
  const comma = last ? null : <span className="json-punct">,</span>;

  if (!isArray && !isObject) {
    return (
      <div className="json-line" style={{ paddingLeft: depth * 14 }}>
        {key}
        <Primitive value={value} />
        {comma}
      </div>
    );
  }

  const entries = isArray
    ? value.map((v, i) => [i, v])
    : Object.entries(value);
  const openB = isArray ? '[' : '{';
  const closeB = isArray ? ']' : '}';
  const count = entries.length;
  const shown = isArray && !showAll ? entries.slice(0, ARRAY_CUT) : entries;
  const hidden = count - shown.length;

  if (count === 0) {
    return (
      <div className="json-line" style={{ paddingLeft: depth * 14 }}>
        {key}
        <span className="json-punct">{openB}{closeB}</span>
        {comma}
      </div>
    );
  }

  return (
    <div role="treeitem" aria-expanded={open}>
      <div className="json-line" style={{ paddingLeft: depth * 14 }}>
        <button
          type="button"
          className="json-fold"
          onClick={() => setOpen((v) => !v)}
          aria-label={open ? 'Collapse' : 'Expand'}
        >
          {open ? '▾' : '▸'}
        </button>
        {key}
        <span className="json-punct">{openB}</span>
        {!open && (
          <>
            <button type="button" className="json-summary" onClick={() => setOpen(true)}>
              {count} {isArray ? (count === 1 ? 'item' : 'items') : (count === 1 ? 'key' : 'keys')}
            </button>
            <span className="json-punct">{closeB}</span>
            {comma}
          </>
        )}
      </div>
      {open && (
        <>
          {shown.map(([k, v], i) => (
            <Node
              key={k}
              name={isArray ? undefined : k}
              value={v}
              depth={depth + 1}
              last={i === shown.length - 1 && hidden === 0}
            />
          ))}
          {hidden > 0 && (
            <div className="json-line" style={{ paddingLeft: (depth + 1) * 14 }}>
              <button type="button" className="json-summary" onClick={() => setShowAll(true)}>
                … {hidden} more
              </button>
            </div>
          )}
          <div className="json-line" style={{ paddingLeft: depth * 14 }}>
            <span className="json-punct">{closeB}</span>
            {comma}
          </div>
        </>
      )}
    </div>
  );
}

function Primitive({ value }) {
  if (value === null) return <span className="json-null">null</span>;
  switch (typeof value) {
    case 'string':
      return <span className="json-string">&quot;{value}&quot;</span>;
    case 'number':
      return <span className="json-number">{String(value)}</span>;
    case 'boolean':
      return <span className="json-bool">{String(value)}</span>;
    default:
      return <span className="json-null">{String(value)}</span>;
  }
}
