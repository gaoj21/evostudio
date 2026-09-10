import React, { useEffect, useRef, useState } from 'react';
import JsonView from '../../components/JsonView.jsx';
import { parseJsonLines } from '../../jsonView.js';
import { api } from '../../api.js';

/**
 * Right-click menu for one workspace entry.
 *
 * Positioned where the click was, closed by the next click or Escape, and
 * flipped back inside the window when the click was near an edge.
 */
function ContextMenu({ menu, onClose, items }) {
  const ref = useRef(null);
  const [pos, setPos] = useState({ left: menu.x, top: menu.y });

  useEffect(() => {
    const box = ref.current?.getBoundingClientRect();
    if (!box) return;
    setPos({
      left: Math.min(menu.x, window.innerWidth - box.width - 8),
      top: Math.min(menu.y, window.innerHeight - box.height - 8),
    });
  }, [menu.x, menu.y]);

  useEffect(() => {
    const away = () => onClose();
    const key = (e) => e.key === 'Escape' && onClose();
    document.addEventListener('mousedown', away);
    document.addEventListener('keydown', key);
    return () => {
      document.removeEventListener('mousedown', away);
      document.removeEventListener('keydown', key);
    };
  }, [onClose]);

  return (
    <div className="ctx-menu" ref={ref} style={pos} onMouseDown={(e) => e.stopPropagation()}>
      <div className="ctx-path" title={menu.path}>{menu.path}</div>
      {items.map((item) => (
        item.separator ? <div key={item.key} className="ctx-sep" /> : (
          <button
            key={item.key}
            type="button"
            className={`ctx-item${item.danger ? ' ctx-danger' : ''}`}
            onClick={() => { onClose(); item.onClick(); }}
          >
            {item.label}
          </button>
        )
      ))}
    </div>
  );
}

const EXPANDED_KEY = 'evoagentx-studio:workspace-open';

function loadExpanded(graphId) {
  try {
    return new Set(JSON.parse(localStorage.getItem(`${EXPANDED_KEY}:${graphId}`)) || []);
  } catch {
    return new Set();   // unavailable or corrupt storage: start folded
  }
}

function saveExpanded(graphId, paths) {
  try {
    localStorage.setItem(`${EXPANDED_KEY}:${graphId}`, JSON.stringify([...paths]));
  } catch {
    /* storage unavailable; the tree still works for this session */
  }
}

/**
 * The flat path listing, as the folder tree it describes.
 *
 * Folders first, then files, each alphabetical. A workspace is a project plus
 * one folder per run with a file per node, and a flat listing of that stops
 * being readable at the second run.
 */
export function buildTree(files) {
  const root = { name: '', path: '', dir: true, children: [] };
  const byPath = new Map([['', root]]);

  const folder = (path) => {
    if (byPath.has(path)) return byPath.get(path);
    const cut = path.lastIndexOf('/');
    const parent = folder(cut === -1 ? '' : path.slice(0, cut));
    const node = { name: path.slice(cut + 1), path, dir: true, children: [] };
    byPath.set(path, node);
    parent.children.push(node);
    return node;
  };

  (files || []).forEach((f) => {
    if (f.dir) {
      folder(f.path);
      return;
    }
    const cut = f.path.lastIndexOf('/');
    folder(cut === -1 ? '' : f.path.slice(0, cut))
      .children.push({ ...f, name: f.path.slice(cut + 1), dir: false });
  });

  const order = (node) => {
    node.children.sort((a, b) => (
      a.dir === b.dir ? a.name.localeCompare(b.name) : (a.dir ? -1 : 1)));
    node.children.forEach((child) => { if (child.dir) order(child); });
  };
  order(root);
  return root.children;
}

// So a collapsed folder still says how much it is hiding.
export function countFiles(node) {
  return node.dir
    ? node.children.reduce((total, child) => total + countFiles(child), 0)
    : 1;
}

function TreeRows({ nodes, depth, expanded, onToggle, selected, onSelect, onDelete, onContext }) {
  return nodes.map((node) => {
    const pad = 8 + depth * 12;
    if (!node.dir) {
      return (
        <div
          key={node.path}
          className={`ws-file${selected === node.path ? ' selected' : ''}`}
          style={{ paddingLeft: pad + 14 }}
          onClick={() => onSelect(node.path)}
          onContextMenu={(e) => onContext(e, node.path, false)}
          title={`${node.path} · ${node.size} bytes · ${node.mtime}`}
        >
          {node.name}
          <span className="muted small">
            {' '}
            {node.size > 1024 ? `${(node.size / 1024).toFixed(1)}K` : `${node.size}B`}
          </span>
          <button
            type="button"
            className="param-del ws-file-del"
            title="Delete file"
            onClick={(e) => { e.stopPropagation(); onDelete(node.path); }}
          >
            ✕
          </button>
        </div>
      );
    }
    const open = expanded.has(node.path);
    const inside = countFiles(node);
    return (
      <React.Fragment key={node.path}>
        <div
          className="ws-folder"
          style={{ paddingLeft: pad }}
          onClick={() => onToggle(node.path)}
          onContextMenu={(e) => onContext(e, node.path, true)}
          title={node.path}
          role="button"
          aria-expanded={open}
          aria-label={`${node.name} folder`}
        >
          <span className="ws-chevron">{open ? '▾' : '▸'}</span>
          {node.name}/
          {!open && inside > 0 && <span className="ws-count">{inside}</span>}
        </div>
        {open && (
          <TreeRows
            nodes={node.children}
            depth={depth + 1}
            expanded={expanded}
            onToggle={onToggle}
            selected={selected}
            onSelect={onSelect}
            onDelete={onDelete}
            onContext={onContext}
          />
        )}
      </React.Fragment>
    );
  });
}

function FileTree({ files, selected, onSelect, onDelete, onContext, expanded, onToggle }) {
  const nodes = buildTree(files);
  return (
    <div className="ws-tree">
      <TreeRows
        nodes={nodes}
        depth={0}
        expanded={expanded}
        onToggle={onToggle}
        selected={selected}
        onSelect={onSelect}
        onDelete={onDelete}
        onContext={onContext}
      />
      {nodes.length === 0 && (
        <div className="muted small">Workspace is empty — save the workflow to compile it here.</div>
      )}
    </div>
  );
}


export default function WorkspacePanel({ open, graphId, onClose }) {
  const [files, setFiles] = useState([]);
  const [selected, setSelected] = useState(null);
  const [menu, setMenu] = useState(null);   // {x, y, path, isDir}
  // Which folders are open, remembered per workflow: the shape you were
  // working in is worth more than a tidy default every time the panel opens.
  const [expanded, setExpanded] = useState(() => loadExpanded(graphId));

  useEffect(() => setExpanded(loadExpanded(graphId)), [graphId]);

  const toggleFolder = (path) => setExpanded((current) => {
    const next = new Set(current);
    if (!next.delete(path)) next.add(path);
    saveExpanded(graphId, next);
    return next;
  });
  const [file, setFile] = useState(null);
  const [error, setError] = useState(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [newFileOpen, setNewFileOpen] = useState(false);
  const [newPath, setNewPath] = useState('files/notes.txt');
  const [newFolderOpen, setNewFolderOpen] = useState(false);
  const [newFolderPath, setNewFolderPath] = useState('files/new-folder');
  const uploadRef = useRef(null);

  const load = () => {
    if (!graphId) return Promise.resolve();
    return api
      .getWorkspace(graphId)
      .then((r) => setFiles(r.files || []))
      .catch((err) => setError(err?.body?.detail || err.message));
  };

  useEffect(() => {
    if (!open || !graphId) return;
    setSelected(null);
    setFile(null);
    setEditing(false);
    load();
    // refresh the tree while docked so new run artifacts show up
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, graphId]);

  useEffect(() => {
    if (!selected || !graphId) return;
    setFile(null);
    setEditing(false);
    api
      .getWorkspaceFile(graphId, selected)
      .then(setFile)
      .catch((err) => setFile({ content: `Error: ${err?.body?.detail || err.message}` }));
  }, [selected, graphId]);

  if (!open) return null;

  const removeFile = async (path, { recursive = false } = {}) => {
    if (!window.confirm(`Delete ${path}?`)) return;
    try {
      await api.deleteWorkspaceFile(graphId, path, recursive);
    } catch (err) {
      const detail = err?.body?.detail || err.message;
      // A folder refuses to go while it holds anything, and says how much:
      // one careless click on runs/ would otherwise take every run with it.
      if (/removes them too/.test(String(detail))
          && window.confirm(`${detail}\n\nDelete it and everything in it?`)) {
        try {
          await api.deleteWorkspaceFile(graphId, path, true);
        } catch (retry) {
          setError(retry?.body?.detail || retry.message);
          return;
        }
      } else {
        setError(detail);
        return;
      }
    }
    if (selected === path || String(selected || '').startsWith(`${path}/`)) {
      setSelected(null);
      setFile(null);
    }
    await load();
  };

  const download = (path) => {
    // A plain link click: the browser saves it, so a whole run or the entire
    // project never passes through JS.
    const a = document.createElement('a');
    a.href = api.workspaceDownloadUrl(graphId, path);
    a.download = '';
    document.body.appendChild(a);
    a.click();
    a.remove();
  };

  const copyPath = async (path) => {
    try {
      await navigator.clipboard.writeText(path);
    } catch {
      setError(`Could not copy — the path is ${path}`);
    }
  };

  const isMemory = (path) => path === 'memory' || path.startsWith('memory/');

  const menuItems = (entry) => (entry.isDir ? [
    { key: 'dl', label: 'Download as zip', onClick: () => download(entry.path) },
    { key: 'copy', label: 'Copy path', onClick: () => copyPath(entry.path) },
    ...(isMemory(entry.path) ? [] : [
      { key: 's1', separator: true },
      { key: 'del', label: 'Delete folder', danger: true,
        onClick: () => removeFile(entry.path) },
    ]),
  ] : [
    { key: 'open', label: 'Open', onClick: () => setSelected(entry.path) },
    { key: 'dl', label: 'Download', onClick: () => download(entry.path) },
    { key: 'copy', label: 'Copy path', onClick: () => copyPath(entry.path) },
    ...(isMemory(entry.path) ? [] : [
      { key: 's1', separator: true },
      { key: 'del', label: 'Delete', danger: true, onClick: () => removeFile(entry.path) },
    ]),
  ]);

  const upload = async (e) => {
    const f = e.target.files?.[0];
    e.target.value = '';
    if (!f) return;
    try {
      await api.uploadWorkspaceFile(graphId, f);
      await load();
    } catch (err) {
      setError(err?.body?.detail || err.message);
    }
  };

  const createFile = async () => {
    try {
      await api.saveWorkspaceFile(graphId, newPath, '');
      setNewFileOpen(false);
      await load();
      setSelected(newPath);
      setEditing(true);
      setDraft('');
    } catch (err) {
      setError(err?.body?.detail || err.message);
    }
  };

  const createFolder = async () => {
    try {
      await api.mkdirWorkspace(graphId, newFolderPath);
      setNewFolderOpen(false);
      await load();
    } catch (err) {
      setError(err?.body?.detail || err.message);
    }
  };

  const saveEdit = async () => {
    try {
      await api.saveWorkspaceFile(graphId, selected, draft);
      setEditing(false);
      await load();
      const refreshed = await api.getWorkspaceFile(graphId, selected);
      setFile(refreshed);
    } catch (err) {
      setError(err?.body?.detail || err.message);
    }
  };

  const pretty = (f) => {
    if (!f) return '';
    if (f.path?.endsWith('.json')) {
      try {
        return JSON.stringify(JSON.parse(f.content), null, 2);
      } catch {
        /* not JSON */
      }
    }
    return f.content;
  };

  return (
    <div className="ws-panel">
      <div className="drawer-head">
        <h3 style={{ margin: 0 }}>Workspace</h3>
        <button onClick={onClose}>✕</button>
      </div>
      <div className="ws-toolbar">
        <button type="button" onClick={() => setNewFileOpen(true)} disabled={!graphId}>
          + New file
        </button>
        <button type="button" onClick={() => setNewFolderOpen(true)} disabled={!graphId}>
          + New folder
        </button>
        <button type="button" onClick={() => uploadRef.current?.click()} disabled={!graphId}>
          ↑ Upload
        </button>
        <input ref={uploadRef} type="file" style={{ display: 'none' }} onChange={upload} />
      </div>
      {error && <div className="muted small batch-error">{String(error)}</div>}
      <div className="ws-tree-wrap">
        <FileTree
          files={files}
          selected={selected}
          onSelect={setSelected}
          onDelete={removeFile}
          expanded={expanded}
          onToggle={toggleFolder}
          onContext={(e, path, isDir) => {
            e.preventDefault();
            setMenu({ x: e.clientX, y: e.clientY, path, isDir });
          }}
        />
        {menu && (
          <ContextMenu menu={menu} onClose={() => setMenu(null)} items={menuItems(menu)} />
        )}
      </div>
      <div className="ws-content">
        {selected ? (
          <>
            <div className="muted small">
              {selected}
              {file?.truncated ? ' · truncated to 100KB' : ''}
              {/* Memory is a view of a vector store, so there is no file to
                  edit — offering the button would only lead to a refusal. */}
              {file?.readonly && ' · read-only'}
              {!editing && !file?.truncated && !file?.readonly && (
                <button
                  type="button"
                  className="ws-edit-btn"
                  onClick={() => {
                    setDraft(file?.content ?? '');
                    setEditing(true);
                  }}
                >
                  Edit
                </button>
              )}
            </div>
            {editing ? (
              <>
                <textarea className="ws-editor" value={draft} onChange={(e) => setDraft(e.target.value)} />
                <div className="modal-actions">
                  <button type="button" onClick={() => setEditing(false)}>Cancel</button>
                  <button type="button" className="primary" onClick={saveEdit}>Save</button>
                </div>
              </>
            ) : (
              <FileView file={file} pretty={pretty} />
            )}
          </>
        ) : (
          <p className="muted small">Select a file to view it.</p>
        )}
      </div>
      {newFileOpen && (
        <div className="modal-backdrop" onClick={() => setNewFileOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3>New workspace file</h3>
            <div className="field">
              <label>Path (relative to workspace)</label>
              <input value={newPath} onChange={(e) => setNewPath(e.target.value)} placeholder="files/notes.txt" />
            </div>
            <div className="modal-actions">
              <button type="button" onClick={() => setNewFileOpen(false)}>Cancel</button>
              <button type="button" className="primary" onClick={createFile}>Create</button>
            </div>
          </div>
        </div>
      )}
      {newFolderOpen && (
        <div className="modal-backdrop" onClick={() => setNewFolderOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3>New workspace folder</h3>
            <div className="field">
              <label>Path (relative to workspace)</label>
              <input value={newFolderPath} onChange={(e) => setNewFolderPath(e.target.value)} placeholder="files/batch-1" />
            </div>
            <div className="modal-actions">
              <button type="button" onClick={() => setNewFolderOpen(false)}>Cancel</button>
              <button type="button" className="primary" onClick={createFolder}>Create</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}


// A .json file is a tree; a .jsonl file (the run artifacts) is one tree per
// line; anything else is text.
function FileView({ file, pretty }) {
  if (!file) return <pre className="json-view ws-viewer">Loading…</pre>;
  const path = file.path || '';
  if (path.endsWith('.jsonl')) {
    return <JsonView value={parseJsonLines(file.content)} className="ws-viewer" label={`${parseJsonLines(file.content).length} lines`} />;
  }
  if (path.endsWith('.json')) {
    return <JsonView value={file.content} className="ws-viewer" />;
  }
  return <pre className="json-view ws-viewer">{pretty(file)}</pre>;
}
