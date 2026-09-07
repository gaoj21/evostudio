import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ReactFlow,
  ReactFlowProvider,
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  useReactFlow,
  applyNodeChanges,
  applyEdgeChanges,
} from '@xyflow/react';
import { api } from './api.js';
import { Group, Panel, Separator, useDefaultLayout } from 'react-resizable-panels';
import { graphToFlow, flowToGraph, uniqueName } from './convert.js';
import { useLayoutMode } from './useLayoutMode.js';
import { BATCH_SETTLED, useExecutionSession } from './useExecutionSession.js';
import { useStudioNavigation } from './useStudioNavigation.js';
import TaskNode from './components/TaskNode.jsx';
import SourceNode from './components/SourceNode.jsx';
import ToolNode from './components/ToolNode.jsx';
import Palette from './components/Palette.jsx';
import ToolsPanel from './components/ToolsPanel.jsx';
import Inspector from './components/Inspector.jsx';
import { memorySiblings } from './components/MemorySettings.jsx';
import RunDialog from './components/RunDialog.jsx';
import MemoryPanel from './components/MemoryPanel.jsx';
import EvolvePanel from './components/EvolvePanel.jsx';
import ReviewPanel from './components/ReviewPanel.jsx';
import WorkspacePanel from './components/WorkspacePanel.jsx';
import ChatPanel, { renameChatHistory } from './components/ChatPanel.jsx';
import RunsPanel from './components/RunsPanel.jsx';
import SchedulePanel from './components/SchedulePanel.jsx';
import TopBar from './components/TopBar.jsx';

const nodeTypes = { task: TaskNode, source: SourceNode, tool: ToolNode };

// Wide enough that the Nodes / Tools / Workspace tabs and the collapse chevron
// all fit without truncating, and that palette descriptions stop wrapping to
// three lines.
const DEFAULT_SIDEBAR_SIZE = 19; // percent of the group
const DEFAULT_INSPECTOR_SIZE = 18;

// `Panel.expand()` restores the panel's most recent size, but that history lives
// in memory only: after reloading a page that was saved collapsed there is none,
// and the panel springs back to `minSize` (a 10% sidebar is too narrow to read).
// So remember the last expanded width ourselves.
const PANEL_SIZE_KEY = 'evoagentx-studio:expanded-panel-sizes';

function readExpandedSizes() {
  try {
    return JSON.parse(localStorage.getItem(PANEL_SIZE_KEY)) || {};
  } catch {
    return {}; // unavailable or corrupt storage: fall back to the defaults
  }
}

const FALLBACK_PALETTE = [
  {
    type: 'agent',
    label: 'Agent',
    description: 'LLM agent task',
    defaults: {
      description: '',
      inputs: [],
      outputs: [{ name: 'result', type: 'str', description: '', required: true }],
      prompt: '',
      system_prompt: '',
      parse_mode: 'str',
    },
  },
];

function extractErrors(err) {
  const detail = err?.body?.detail;
  if (Array.isArray(detail)) {
    return detail.map((d) => (typeof d === 'string' ? d : d.msg || JSON.stringify(d)));
  }
  if (typeof detail === 'string') return [detail];
  return [err.message || 'Unknown error'];
}


const isSettled = (settled, status) => settled.includes(status);

// Stable ids for the three main panels. Kept module-level so the array identity
// never changes: `useDefaultLayout` memoises on it, and the ids are part of the
// localStorage key, so a new identity (or a renamed panel) would silently drop
// the saved layout.
const MAIN_PANEL_IDS = ['sidebar', 'canvas', 'inspector'];

export function Studio() {
  const [palette, setPalette] = useState([]);
  const [sourcePalette, setSourcePalette] = useState([]);
  const [graphs, setGraphs] = useState([]);
  const [graph, setGraph] = useState(null); // {id, name, goal}
  const [workflowInputs, setWorkflowInputs] = useState([]);
  const [nodes, setNodes] = useNodesState([]);
  const [edges, setEdges] = useEdgesState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [errors, setErrors] = useState(null);
  const [notices, setNotices] = useState(null);
  const [saving, setSaving] = useState(false);
  const [activeOverlay, setActiveOverlay] = useState(null);
  const [copied, setCopied] = useState(false);
  const layout = useLayoutMode();
  const {
    closeLibrary,
    compactPane,
    leftTab,
    libraryOpen,
    openLeft,
    openRight,
    openWorkspace,
    revealSelection,
    rightTab,
    showCompact,
    toggleLibrary,
  } = useStudioNavigation(layout);
  const [dirty, setDirty] = useState(false);
  const [confirmState, setConfirmState] = useState(null);
  // Serialisation of the last saved/loaded state. Comparing against it beats a
  // "touched" flag: undoing back to the saved state correctly reports clean.
  const cleanRef = useRef(null);
  // React StrictMode deliberately replays mount effects in development. Keep
  // project initialization idempotent so an empty Studio does not create two
  // "Untitled Workflow" documents on first load.
  const initializedRef = useRef(false);
  // Set when Chat persists the graph server-side. The dirty effect consumes it
  // on its next pass, once the canvas state that was saved has actually landed
  // — marking clean inline would race the apply that happened in the same tick.
  const pendingCleanRef = useRef(false);
  // Undo history for the canvas. Snapshots are taken on settled changes rather
  // than per mutation, so a node drag collapses into one undo step and edits
  // made by Chat are covered without instrumenting every call site.
  const history = useRef({ past: [], future: [], last: null, suppress: false });
  const [graphTemplates, setGraphTemplates] = useState([]);
  const [watchInfo, setWatchInfo] = useState(null); // {watching, watchers}
  const { screenToFlowPosition } = useReactFlow();
  const idRef = useRef(0);
  const leftPanelRef = useRef(null);
  const rightPanelRef = useRef(null);
  const [leftCollapsed, setLeftCollapsed] = useState(false);
  const [rightCollapsed, setRightCollapsed] = useState(false);

  const reportExecutionError = useCallback((error) => {
    setErrors(Array.isArray(error) ? error : extractErrors(error));
  }, []);
  const clearSelection = useCallback(() => setSelectedId(null), []);
  const openOverlay = useCallback((name) => setActiveOverlay(name), []);
  const closeOverlay = useCallback(() => setActiveOverlay(null), []);
  const execution = useExecutionSession({
    graphId: graph?.id,
    onError: reportExecutionError,
    onClearSelection: clearSelection,
  });
  const {
    abandonRun,
    batch,
    batchProgress,
    beginBatch,
    cancelBatch,
    drawerOpen,
    drawerTab,
    exitRunMode,
    launchRun,
    openPastBatch: openExecutionBatch,
    openPastRun: openExecutionRun,
    reset: resetExecution,
    run,
    runByName,
    runMode,
    runStarting,
    setDrawerOpen,
    setDrawerTab,
    unattended,
  } = execution;
  // Remembers panel widths *and* collapsed state across reloads (localStorage).
  // A restored layout of 0 collapses the panel; `onResize` fires on mount via
  // the library's ResizeObserver, so the edge expand button appears with it.
  const { defaultLayout, onLayoutChanged } = useDefaultLayout({
    id: 'studio-main',
    panelIds: MAIN_PANEL_IDS,
  });
  const expandedSizes = useRef(readExpandedSizes());

  const onPanelResize = useCallback((id, size, setCollapsed) => {
    const pct = size.asPercentage;
    const collapsed = pct < 1;
    if (collapsed) {
      // Persist on the collapse transition only: the value is needed just by a
      // reload that happens while collapsed, and this avoids a localStorage
      // write per pointer move while dragging the separator.
      try {
        localStorage.setItem(PANEL_SIZE_KEY, JSON.stringify(expandedSizes.current));
      } catch {
        /* storage unavailable; expanding falls back to the default size */
      }
    } else {
      expandedSizes.current[id] = pct;
    }
    setCollapsed(collapsed);
  }, []);

  const expandPanel = useCallback((panelRef, id, fallback) => {
    const panel = panelRef.current;
    if (!panel) return;
    panel.expand();
    // A unitless string is read as a percentage; a number would mean pixels.
    panel.resize(`${expandedSizes.current[id] ?? fallback}`);
  }, []);

  const copyText = useCallback(async (text) => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      return; // clipboard blocked (insecure origin / permission): stay silent
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }, []);

  // ---- undo / dirty ----
  const HISTORY_LIMIT = 60;
  const SETTLE_MS = 350;

  const resetHistory = useCallback((n, e) => {
    history.current = { past: [], future: [], last: { nodes: n, edges: e }, suppress: false };
  }, []);

  const markClean = useCallback((meta, n, e) => {
    cleanRef.current = JSON.stringify(flowToGraph(meta, n, e));
    setDirty(false);
  }, []);

  useEffect(() => {
    const h = history.current;
    if (h.suppress) {
      h.suppress = false;
      h.last = { nodes, edges };
      return undefined;
    }
    if (h.last === null) {
      h.last = { nodes, edges };
      return undefined;
    }
    if (h.last.nodes === nodes && h.last.edges === edges) return undefined;
    const previous = h.last;
    const timer = setTimeout(() => {
      h.past.push(previous);
      if (h.past.length > HISTORY_LIMIT) h.past.shift();
      h.future = [];
      h.last = { nodes, edges };
    }, SETTLE_MS);
    return () => clearTimeout(timer);
  }, [nodes, edges]);

  useEffect(() => {
    if (!graph || cleanRef.current === null) return;
    const current = JSON.stringify(flowToGraph(graph, nodes, edges));
    if (pendingCleanRef.current) {
      pendingCleanRef.current = false;
      cleanRef.current = current;
      setDirty(false);
      return;
    }
    setDirty(current !== cleanRef.current);
  }, [graph, nodes, edges]);

  const restore = useCallback(
    (from, to) => {
      const h = history.current;
      if (!h[from].length) return;
      h[to].push(h.last);
      const snapshot = h[from].pop();
      h.suppress = true;
      h.last = snapshot;
      setNodes(snapshot.nodes);
      setEdges(snapshot.edges);
      setSelectedId(null);
    },
    [setNodes, setEdges]
  );

  const undo = useCallback(() => restore('past', 'future'), [restore]);
  const redo = useCallback(() => restore('future', 'past'), [restore]);

  // Promise-based confirmation so callers can `await` a decision inline.
  const confirm = useCallback(
    (options) => new Promise((resolve) => setConfirmState({ ...options, resolve })),
    []
  );

  const resolveConfirm = useCallback((answer) => {
    setConfirmState((c) => {
      if (c) c.resolve(answer);
      return null;
    });
  }, []);

  const confirmDiscard = useCallback(
    async (what) => {
      if (!dirty) return true;
      return confirm({
        title: 'Discard unsaved changes?',
        body: `This workflow has unsaved edits. ${what} will lose them.`,
        confirmLabel: 'Discard',
        danger: true,
      });
    },
    [dirty, confirm]
  );

  // ---- palette ----
  useEffect(() => {
    api
      .palette()
      .then((p) => {
        setPalette(p.templates?.length ? p.templates : FALLBACK_PALETTE);
        setSourcePalette(p.sources || []);
      })
      .catch(() => setPalette(FALLBACK_PALETTE));
    api
      .listTemplates()
      .then((r) => setGraphTemplates(r.templates || []))
      .catch(() => setGraphTemplates([]));
  }, []);

  // ---- graph loading ----
  const openGraph = useCallback(
    async (id) => {
      const g = await api.getGraph(id);
      const { nodes: n, edges: e } = graphToFlow(g);
      const meta = { id: g.id, name: g.name, goal: g.goal || '',
                     output_dir: g.output_dir || 'runs',
                     preprocess: g.preprocess || null };
      setGraph(meta);
      setWorkflowInputs(g.workflow_inputs || []);
      setNodes(n);
      setEdges(e);
      resetHistory(n, e);
      markClean(meta, n, e);
      setSelectedId(null);
      setErrors(null);
      setNotices(null);
      resetExecution();
      api.getWatch(id).then(setWatchInfo).catch(() => setWatchInfo(null));
    },
    [setNodes, setEdges, markClean, resetHistory, resetExecution]
  );

  const refreshList = useCallback(async () => {
    const list = await api.listGraphs();
    const sorted = [...list].sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')));
    setGraphs(sorted);
    return sorted;
  }, []);

  const newGraph = useCallback(async () => {
    const g = await api.createGraph('Untitled Workflow', '');
    await refreshList();
    await openGraph(g.id);
  }, [refreshList, openGraph]);

  // ---- template loading ----
  const loadTemplate = useCallback(
    async (templateId) => {
      if (!(await confirmDiscard('Loading a template'))) return;
      const t = await api.getTemplate(templateId);
      const g = await api.createGraph(t.graph.name, t.graph.goal);
      await api.saveGraph(g.id, t.graph);
      await refreshList();
      await openGraph(g.id);
    },
    [refreshList, openGraph, confirmDiscard]
  );

  useEffect(() => {
    if (initializedRef.current) return;
    initializedRef.current = true;
    (async () => {
      try {
        const list = await refreshList();
        if (list.length) {
          await openGraph(list[0].id);
        } else {
          await newGraph();
        }
      } catch (err) {
        setErrors([`Could not reach backend: ${err.message}. Canvas is in offline scratch mode.`]);
        setGraph({ id: null, name: 'Untitled Workflow', goal: '' });
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---- canvas change handlers ----
  const deleteNode = useCallback(
    (id) => {
      setNodes((nds) => nds.filter((n) => n.id !== id));
      setEdges((eds) => eds.filter((e) => e.source !== id && e.target !== id));
      setSelectedId((sel) => (sel === id ? null : sel));
    },
    [setNodes, setEdges]
  );

  const onNodesChange = useCallback(
    (changes) => {
      const removed = changes.filter((c) => c.type === 'remove').map((c) => c.id);
      if (removed.length) {
        setEdges((eds) => eds.filter((e) => !removed.includes(e.source) && !removed.includes(e.target)));
        setSelectedId((sel) => (removed.includes(sel) ? null : sel));
      }
      setNodes((nds) => applyNodeChanges(changes, nds));
    },
    [setNodes, setEdges]
  );

  const onEdgesChange = useCallback((changes) => setEdges((eds) => applyEdgeChanges(changes, eds)), [setEdges]);

  const onConnect = useCallback(
    (conn) => {
      if (!conn.source || !conn.target || conn.source === conn.target) return;
      const id = `e:${conn.source}->${conn.target}`;
      setEdges((eds) => (eds.some((e) => e.id === id) ? eds : [...eds, { id, source: conn.source, target: conn.target }]));
    },
    [setEdges]
  );

  const onSelectionChange = useCallback(({ nodes: sel }) => {
    const id = sel.length ? sel[0].id : null;
    setSelectedId(id);
    revealSelection(id);
  }, [revealSelection]);

  // ---- add nodes from palette ----
  const addNode = useCallback(
    (tpl, position) => {
      setNodes((nds) => {
        const taken = new Set(nds.map((n) => n.id));
        const name = uniqueName(tpl.type || tpl.label || 'node', taken);
        idRef.current += 1;
        const pos = position || { x: 120 + (idRef.current % 8) * 40, y: 100 + (idRef.current % 8) * 40 };
        const defaults = tpl.defaults || {};
        if (defaults.kind === 'source') {
          return [
            ...nds,
            {
              id: name,
              type: 'source',
              position: pos,
              data: {
                kind: 'source',
                description: defaults.description || tpl.description || '',
                inputs: [],
                outputs: defaults.outputs ? JSON.parse(JSON.stringify(defaults.outputs)) : [],
                source: { ...(defaults.source || { type: 'credit_risk', split: '', n: 1, seed: 42 }) },
              },
            },
          ];
        }
        if (defaults.kind === 'tool') {
          return [
            ...nds,
            {
              id: name,
              type: 'tool',
              position: pos,
              data: {
                kind: 'tool',
                description: defaults.description || tpl.description || '',
                tool: defaults.tool || '',
                inputs: defaults.inputs ? JSON.parse(JSON.stringify(defaults.inputs)) : [],
                outputs: [{ name: 'result', type: 'str', description: 'Tool result', required: true }],
              },
            },
          ];
        }
        return [
          ...nds,
          {
            id: name,
            type: 'task',
            position: pos,
            data: {
              description: defaults.description || tpl.description || '',
              inputs: defaults.inputs ? JSON.parse(JSON.stringify(defaults.inputs)) : [],
              outputs: defaults.outputs ? JSON.parse(JSON.stringify(defaults.outputs)) : [],
              prompt: defaults.prompt || '',
              system_prompt: defaults.system_prompt || '',
              parse_mode: defaults.parse_mode || 'str',
              tool_names: defaults.tool_names || [],
              use_long_term_memory: !!defaults.use_long_term_memory,
            },
          },
        ];
      });
    },
    [setNodes]
  );

  const onDrop = useCallback(
    (e) => {
      e.preventDefault();
      const raw = e.dataTransfer.getData('application/evoagentx-template');
      if (!raw) return;
      try {
        const tpl = JSON.parse(raw);
        addNode(tpl, screenToFlowPosition({ x: e.clientX, y: e.clientY }));
      } catch {
        /* ignore bad drag payload */
      }
    },
    [addNode, screenToFlowPosition]
  );

  const onDragOver = useCallback((e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
  }, []);

  // ---- inspector edits ----
  const updateNode = useCallback(
    (id, patch) => {
      setNodes((nds) => nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, ...patch } } : n)));
    },
    [setNodes]
  );

  const renameNode = useCallback(
    (id, rawName) => {
      const taken = new Set(nodes.map((n) => n.id));
      taken.delete(id);
      const next = uniqueName(rawName, taken);
      setNodes((nds) =>
        nds.map((n) => {
          if (n.id !== id) return n;
          const { editName, ...rest } = n.data;
          return { ...n, id: next, data: rest };
        })
      );
      if (next !== id) {
        setEdges((eds) =>
          eds.map((e) => ({
            ...e,
            id: `e:${e.source === id ? next : e.source}->${e.target === id ? next : e.target}`,
            source: e.source === id ? next : e.source,
            target: e.target === id ? next : e.target,
          }))
        );
        setSelectedId((sel) => (sel === id ? next : sel));
      }
    },
    [nodes, setNodes, setEdges]
  );

  // ---- top bar actions ----
  const selectGraph = useCallback(
    (id) => {
      if (!id || id === graph?.id) return;
      (async () => {
        if (!(await confirmDiscard('Switching workflow'))) return;
        try {
          await openGraph(id);
        } catch (err) {
          setErrors(extractErrors(err));
        }
      })();
    },
    [graph?.id, openGraph, confirmDiscard]
  );

  const onNew = useCallback(() => {
    (async () => {
      if (!(await confirmDiscard('Creating a new workflow'))) return;
      newGraph().catch((err) => setErrors(extractErrors(err)));
    })();
  }, [newGraph, confirmDiscard]);

  const onDeleteGraph = useCallback(() => {
    if (!graph?.id) return;
    (async () => {
      // Deleting removes the workflow's JSON file with no way back.
      const ok = await confirm({
        title: `Delete "${graph.name || graph.id}"?`,
        body: 'The workflow is deleted permanently. Runs and workspace files it '
          + 'already produced are kept.',
        confirmLabel: 'Delete',
        danger: true,
      });
      if (!ok) return;
      try {
        await api.deleteGraph(graph.id);
        const list = await refreshList();
        if (list.length) await openGraph(list[0].id);
        else await newGraph();
      } catch (err) {
        setErrors(extractErrors(err));
      }
    })();
  }, [graph?.id, graph?.name, refreshList, openGraph, newGraph, confirm]);

  // Renaming is its own action, not a save: it moves the workflow's identity
  // and everything filed under it, and must not drag whatever is unsaved on
  // the canvas along with it.
  const onRename = useCallback(async (name) => {
    if (!graph?.id || name === graph.name) return;
    setErrors(null);
    try {
      const renamed = await api.renameGraph(graph.id, name);
      const meta = { ...graph, id: renamed.id, name: renamed.name };
      if (renamed.id !== graph.id) renameChatHistory(graph.id, renamed.id);
      setGraph(meta);
      // A clean canvas stays clean: its baseline holds the old name, so
      // without this the rename alone would leave it looking edited.
      if (!dirty) markClean(meta, nodes, edges);
      refreshList();
    } catch (err) {
      setErrors(extractErrors(err));
    }
  }, [graph, dirty, nodes, edges, markClean, refreshList]);

  const saveCurrent = useCallback(async () => {
    if (!graph?.id) return null;
    const body = flowToGraph(graph, nodes, edges);
    const saved = await api.saveGraph(graph.id, body);
    setWorkflowInputs(saved.workflow_inputs || []);
    // Renaming a workflow renames its identity too, so the id that comes back
    // may not be the one that went out. Everything downstream — run history,
    // the workspace, exports — is keyed on it, so follow it.
    if (saved.id !== graph.id) renameChatHistory(graph.id, saved.id);
    const meta = { ...graph, id: saved.id, name: saved.name, goal: saved.goal || '',
                   output_dir: saved.output_dir || 'runs',
                   preprocess: saved.preprocess || null };
    setGraph(meta);
    markClean(meta, nodes, edges);
    refreshList();
    return meta;
  }, [graph, nodes, edges, refreshList, markClean]);

  const onSaveRef = useRef(null);

  const onSave = useCallback(async () => {
    if (!graph?.id) return;
    setSaving(true);
    setErrors(null);
    try {
      await saveCurrent();
    } catch (err) {
      setErrors(extractErrors(err));
    } finally {
      setSaving(false);
    }
  }, [graph?.id, saveCurrent]);

  onSaveRef.current = onSave;

  // ---- keyboard + unload guard ----
  useEffect(() => {
    const onKey = (e) => {
      const el = e.target;
      // Text fields keep their own native undo stack; never hijack those.
      const typing = el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable);
      const mod = e.metaKey || e.ctrlKey;
      if (!mod) return;
      if (e.key === 's') {
        e.preventDefault();
        if (!runMode) onSaveRef.current?.();
        return;
      }
      if (typing || runMode) return;
      if (e.key === 'z' && !e.shiftKey) {
        e.preventDefault();
        undo();
      } else if ((e.key === 'z' && e.shiftKey) || e.key === 'y') {
        e.preventDefault();
        redo();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [undo, redo, runMode]);

  useEffect(() => {
    if (!dirty) return undefined;
    const onBeforeUnload = (e) => {
      e.preventDefault();
      e.returnValue = '';
    };
    window.addEventListener('beforeunload', onBeforeUnload);
    return () => window.removeEventListener('beforeunload', onBeforeUnload);
  }, [dirty]);

  // Import lands in a new workflow, but opening it still replaces the canvas
  // currently on screen. Guard that transition exactly like New and Select.
  const importInputRef = useRef(null);

  const onImportFile = useCallback(
    async (file) => {
      if (!file) return;
      if (!(await confirmDiscard('Importing a project'))) return;
      try {
        const imported = await api.importGraph(file);
        await refreshList();
        await openGraph(imported.id);
        const notes = [
          ...(imported.imported_tools?.length
            ? [`installed tools: ${imported.imported_tools.join(', ')}`] : []),
          ...(imported.imported_skills?.length
            ? [`installed skills: ${imported.imported_skills.join(', ')}`] : []),
          ...(imported.notes || []),
        ];
        if (notes.length) setNotices(notes);
      } catch (err) {
        setErrors(extractErrors(err));
      }
    },
    [refreshList, openGraph, confirmDiscard]
  );

  // Export runs off the *saved* graph, so persist first — otherwise the
  // downloaded project would not match what is on screen.
  const onExport = useCallback(async () => {
    if (!graph?.id) return;
    try {
      const current = dirty ? await saveCurrent() : graph;
      if (!current?.id) return;
      window.location.assign(`/api/graphs/${encodeURIComponent(current.id)}/export`);
    } catch (err) {
      setErrors(extractErrors(err));
    }
  }, [graph, dirty, saveCurrent]);

  // ---- chat ----
  // The chat reasons about the canvas as the user currently sees it, unsaved
  // edits included, and its result is applied the same way: to the canvas only.
  // Saving stays an explicit act, so a bad suggestion is never persisted.
  const chatSnapshot = useCallback(
    () => flowToGraph(graph, nodes, edges),
    [graph, nodes, edges]
  );

  const openPastRun = useCallback(
    async (listedRun) => {
      closeOverlay();
      await openExecutionRun(listedRun);
    },
    [closeOverlay, openExecutionRun]
  );

  // Reopening a batch is also how a still-running one is picked back up after
  // a reload: the progress poll keys on the id, so putting it back into state
  // reattaches the canvas to work that never stopped.
  const openPastBatch = useCallback(
    async (listedBatch) => {
      closeOverlay();
      await openExecutionBatch(listedBatch);
    },
    [closeOverlay, openExecutionBatch]
  );

  const applyChatGraph = useCallback(
    (g) => {
      const { nodes: n, edges: e } = graphToFlow(g);
      setNodes(n);
      setEdges(e);
      setGraph((prev) => ({
        ...prev,
        name: g.name ?? prev.name,
        goal: g.goal ?? prev.goal,
      }));
      setSelectedId(null);
      setErrors(null);
    },
    [setNodes, setEdges]
  );

  // ---- run ----
  const startRun = useCallback(
    async (inputs, startAt, session) => {
      if (!graph?.id) return;
      const runId = await launchRun(async () => {
        // Run what you see: persist the canvas before starting the run,
        // otherwise the server executes the last *saved* version.
        const current = await saveCurrent();
        if (!current?.id) return null;
        const { run_id: runId } = await api.runGraph(current.id, inputs, startAt, session);
        return { run_id: runId, status: 'running', nodes: [], result: null, error: null };
      });
      closeOverlay();
      return runId;
    },
    [graph?.id, launchRun, saveCurrent, closeOverlay]
  );

  // ---- batch run: canvas-level progress ----
  const onBatchStart = useCallback((batchId) => {
    // Close the dialog: aggregate progress lives on the canvas (per-node
    // counters plus a status badge) and per-record detail in the drawer.
    closeOverlay();
    beginBatch(batchId);
  }, [beginBatch, closeOverlay]);

  // ---- watch (scheduled source nodes) ----
  const onToggleWatch = useCallback(async () => {
    if (!graph?.id) return;
    try {
      if (watchInfo?.watching) {
        await api.stopWatch(graph.id);
        setWatchInfo({ watching: false, watchers: [] });
      } else {
        const current = await saveCurrent();
        if (!current?.id) return;
        setWatchInfo(await api.startWatch(current.id));
      }
    } catch (err) {
      setErrors(extractErrors(err));
    }
  }, [graph?.id, watchInfo?.watching, saveCurrent]);

  const openEvolve = useCallback(async () => {
    if (!graph?.id) return;
    setErrors(null);
    try {
      // Optimization runs on the server-side graph, so make sure it is the
      // same version the user is looking at before opening the panel.
      if (dirty) await saveCurrent();
      openOverlay('evolve');
    } catch (err) {
      setErrors(extractErrors(err));
    }
  }, [graph?.id, dirty, saveCurrent, openOverlay]);

  const openSchedule = useCallback(async () => {
    if (!graph?.id) return;
    setErrors(null);
    try {
      // A schedule executes the stored workflow later, not the live canvas.
      if (dirty) await saveCurrent();
      openOverlay('schedule');
    } catch (err) {
      setErrors(extractErrors(err));
    }
  }, [graph?.id, dirty, saveCurrent, openOverlay]);

  // poll watch status while watching (fires may trigger runs)
  useEffect(() => {
    if (!watchInfo?.watching || !graph?.id) return undefined;
    const t = setInterval(() => {
      api.getWatch(graph.id).then(setWatchInfo).catch(() => {});
    }, 5000);
    return () => clearInterval(t);
  }, [watchInfo?.watching, graph?.id]);

  // ---- derived ----
  const displayNodes = useMemo(
    () => {
      // nodes with no edges at all are parked drafts (inert at run time)
      const connected = new Set();
      edges.forEach((e) => { connected.add(e.source); connected.add(e.target); });
      const progress = batch?.node_progress || null;
      const watchingNodes = new Set((watchInfo?.watchers || []).map((w) => w.node));
      return nodes.map((n) => {
        let runStatus = null;
        let batchBadge = null;
        if (runMode) {
          if (progress) {
            const p = progress[n.id];
            if (p) {
              const total = p.completed + p.running + p.failed + p.pending;
              runStatus = p.running > 0 ? 'running'
                : p.failed > 0 ? 'failed'
                : total > 0 && p.completed === total ? 'completed'
                : 'pending';
              batchBadge = `${p.completed}/${total}${p.failed ? ` ·${p.failed}✗` : ''}`;
            } else {
              runStatus = 'pending';
            }
          } else {
            runStatus = runByName[n.id]?.status || 'pending';
          }
        }
        return {
          ...n,
          className: connected.has(n.id) ? '' : 'parked',
          data: {
            ...n.data,
            onDelete: deleteNode,
            runMode,
            runStatus,
            batchBadge,
            watching: watchingNodes.has(n.id),
          },
        };
      });
    },
    [nodes, edges, deleteNode, runMode, runByName, batch, watchInfo]
  );

  const selectedNode = nodes.find((n) => n.id === selectedId) || null;

  // Names some node produces. An input that matches none of them is not wired
  // to anything and will be asked for at run time — usually intended, but it is
  // also exactly what a typo'd input name looks like.
  const producedNames = useMemo(() => {
    const names = new Set();
    nodes.forEach((n) => (n.data?.outputs || []).forEach((o) => o.name && names.add(o.name)));
    return names;
  }, [nodes]);

  // Every LLM node and what it declares, so a node choosing whose memory to
  // read can be offered the real list rather than a free-text box.
  const siblings = useMemo(() => memorySiblings(nodes), [nodes]);

  const hasCanvasSource = useMemo(
    () => nodes.some((n) => n.data?.kind === 'source'),
    [nodes]
  );

  // The same pane bodies serve every layout; only their container changes.
  // `collapsible` adds the desktop-only collapse chevron.
  const renderSidebar = (collapsible) => (
    <div className="left-sidebar">
      <div className="sidebar-tabs">
        <button type="button" className={leftTab === 'nodes' ? 'primary' : ''} onClick={() => openLeft('nodes')}>
          Nodes
        </button>
        <button type="button" className={leftTab === 'tools' ? 'primary' : ''} onClick={() => openLeft('tools')}>
          Tools
        </button>
        <button type="button" className={leftTab === 'workspace' ? 'primary' : ''} onClick={openWorkspace}>
          Workspace
        </button>
        {collapsible && (
          <button
            type="button"
            className="collapse-btn"
            title="Collapse panel"
            onClick={() => leftPanelRef.current?.collapse()}
          >
            ◀
          </button>
        )}
      </div>
      <div className="sidebar-body">
        {leftTab === 'nodes' && (
          <Palette templates={palette} sources={sourcePalette} graphTemplates={graphTemplates} onAdd={(tpl) => addNode(tpl)} onLoadTemplate={(id) => loadTemplate(id).catch((err) => setErrors(extractErrors(err)))} disabled={runMode} />
        )}
        {leftTab === 'tools' && <ToolsPanel onAdd={(tpl) => addNode(tpl)} disabled={runMode} />}
        {leftTab === 'workspace' && (
          <WorkspacePanel open graphId={graph?.id} onClose={() => openLeft('nodes')} />
        )}
      </div>
    </div>
  );

  const renderCanvas = (collapsible) => (
    <div className="canvas">
      {collapsible && leftCollapsed && (
        <button
          type="button"
          className="expand-btn expand-left"
          title="Show panel"
          onClick={() => expandPanel(leftPanelRef, 'sidebar', DEFAULT_SIDEBAR_SIZE)}
        >
          ▶
        </button>
      )}
      {collapsible && rightCollapsed && (
        <button
          type="button"
          className="expand-btn expand-right"
          title="Show inspector"
          onClick={() => expandPanel(rightPanelRef, 'inspector', DEFAULT_INSPECTOR_SIZE)}
        >
          ◀
        </button>
      )}
      <ReactFlow
        nodes={displayNodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={runMode ? undefined : onNodesChange}
        onEdgesChange={runMode ? undefined : onEdgesChange}
        onConnect={runMode ? undefined : onConnect}
        onSelectionChange={onSelectionChange}
        onDrop={runMode ? undefined : onDrop}
        onDragOver={onDragOver}
        deleteKeyCode={runMode ? null : ['Delete', 'Backspace']}
        nodesDraggable={!runMode}
        nodesConnectable={!runMode}
        fitView
      >
        <Background />
        <Controls />
        {layout === 'desktop' && <MiniMap pannable zoomable />}
      </ReactFlow>
      {runMode && batch && (
        <div className="run-badge-group">
          <button
            type="button"
            title="Show the records"
            onClick={() => setDrawerOpen((o) => !o)}
            className={`run-badge ${
              batch.status === 'completed' ? 'run-success'
                : batch.status === 'cancelled' ? 'run-stopped' : 'run-running'}`}
          >
            {batchProgress.total
              ? `batch ${batchProgress.done}/${batchProgress.total}`
              : `batch ${batch.status}`}
            {batch.status === 'cancelled' && ' · stopped'}
            {batchProgress.failed > 0 && ` · ${batchProgress.failed}✗`}
            {batch.summary?.mean != null && ` · score ${batch.summary.mean}`}
          </button>
          {!isSettled(BATCH_SETTLED, batch.status) && (
            <button
              type="button"
              className="run-badge run-stop"
              disabled={batch.status === 'cancelling'}
              title="Stop starting new records. Those already running will finish."
              onClick={cancelBatch}
            >
              {batch.status === 'cancelling' ? 'Stopping…' : 'Stop'}
            </button>
          )}
        </div>
      )}
      {runMode && unattended && (
        // Naming the id matters: the whole failure was two batches and one
        // Stop button, with nothing on screen saying which it belonged to.
        <div className="run-badge-group">
          <button
            type="button"
            className="run-badge run-stop"
            title={`Batch ${unattended} is still running and is not the one shown here.`}
            onClick={() => cancelBatch(unattended)}
          >
            {`stop batch ${unattended.slice(0, 8)} (still running)`}
          </button>
        </div>
      )}
      {runMode && !batch && run && (
        <div className="run-badge-group">
          <button
            type="button"
            title={run.status === 'running' ? 'Running…' : 'Show the result'}
            disabled={run.status === 'running'}
            onClick={() => setDrawerOpen((o) => !o)}
            className={`run-badge run-${run.status}`}
          >
            run {run.status}
          </button>
          {run.status === 'running' && (
            <button
              type="button"
              className="run-badge run-stop"
              title={'Stop waiting on this run. It cannot be interrupted, so it '
                + 'finishes in the background and still spends what it has left.'}
              onClick={abandonRun}
            >
              Give up
            </button>
          )}
        </div>
      )}
    </div>
  );

  // In compact layouts the bottom bar already chooses the pane, so the tab row
  // would be a second control for the same state — drive it from `forcedTab`.
  const renderRight = (collapsible, forcedTab) => (
    <div className="inspector-wrap">
      {!forcedTab && (
      <div className="sidebar-tabs">
        <button
          type="button"
          className={rightTab === 'inspector' ? 'primary' : ''}
          onClick={() => openRight('inspector')}
        >
          Inspector
        </button>
        <button
          type="button"
          className={rightTab === 'chat' ? 'primary' : ''}
          onClick={() => openRight('chat')}
        >
          Chat
        </button>
        {collapsible && (
          <button
            type="button"
            className="collapse-btn"
            title="Collapse panel"
            onClick={() => rightPanelRef.current?.collapse()}
          >
            ▶
          </button>
        )}
      </div>
      )}
      <div className="sidebar-body">
        {(forcedTab || rightTab) === 'inspector' ? (
          <Inspector
            node={selectedNode}
            runInfo={selectedNode ? runByName[selectedNode.id] : null}
            runMode={runMode}
            onUpdate={updateNode}
            onRename={renameNode}
            graph={graph}
            onGraphChange={(patch) => setGraph((g) => ({ ...g, ...patch }))}
            producedNames={producedNames}
            siblings={siblings}
          />
        ) : (
          <ChatPanel
            graphId={graph?.id}
            getGraph={chatSnapshot}
            onApply={applyChatGraph}
            onSaved={() => { pendingCleanRef.current = true; refreshList(); }}
            onRunRequest={startRun}
            runOutcome={runMode && (run?.status === 'success' || run?.status === 'failed') ? run : null}
            disabled={false}
          />
        )}
      </div>
    </div>
  );

  return (
    <div className="app">
      <TopBar
        graphs={graphs}
        graphId={graph?.id}
        runMode={runMode}
        saving={saving}
        dirty={dirty}
        compact={layout !== 'desktop'}
        onSelectGraph={selectGraph}
        onNew={onNew}
        onDelete={onDeleteGraph}
        onSave={onSave}
        onRun={() => openOverlay('run')}
        onEvolve={openEvolve}
        onReview={() => openOverlay('review')}
        watching={!!watchInfo?.watching}
        onToggleWatch={onToggleWatch}
        onWorkspace={openWorkspace}
        onExport={onExport}
        onImport={() => importInputRef.current?.click()}
        onRuns={() => openOverlay('runs')}
        onSchedule={openSchedule}
        onRename={onRename}
        onBackToEdit={exitRunMode}
      />
      {errors && (
        <div className="error-banner">
          <div>
            {errors.map((e, i) => (
              <div key={i}>• {e}</div>
            ))}
          </div>
          <button onClick={() => setErrors(null)}>✕</button>
        </div>
      )}
      {notices && (
        <div className="notice-banner">
          <div>
            {notices.map((notice, i) => (
              <div key={i}>• {notice}</div>
            ))}
          </div>
          <button onClick={() => setNotices(null)}>✕</button>
        </div>
      )}
      {layout === 'desktop' ? (
        <Group
          orientation="horizontal"
          className="main"
          defaultLayout={defaultLayout}
          onLayoutChanged={onLayoutChanged}
        >
          <Panel
            id="sidebar"
            defaultSize={`${DEFAULT_SIDEBAR_SIZE}`}
            minSize="10"
            className="panel-fill"
            collapsible
            collapsedSize={0}
            panelRef={leftPanelRef}
            onResize={(size) => onPanelResize('sidebar', size, setLeftCollapsed)}
          >
            {renderSidebar(true)}
          </Panel>
          {!leftCollapsed && <Separator className="resize-handle" />}
          <Panel id="canvas" minSize="25" className="panel-fill">
            {renderCanvas(true)}
          </Panel>
          {!rightCollapsed && <Separator className="resize-handle" />}
          <Panel
            id="inspector"
            defaultSize={`${DEFAULT_INSPECTOR_SIZE}`}
            minSize="10"
            className="panel-fill"
            collapsible
            collapsedSize={0}
            panelRef={rightPanelRef}
            onResize={(size) => onPanelResize('inspector', size, setRightCollapsed)}
          >
            {renderRight(true)}
          </Panel>
        </Group>
      ) : (
        <div className={`main compact compact-${layout}`}>
          {/* Tablet (an unfolded foldable) fits the canvas next to one side
              pane; a phone shows exactly one pane at a time. */}
          {(layout === 'tablet' || compactPane === 'canvas') && (
            <div className="compact-canvas">{renderCanvas(false)}</div>
          )}
          {(layout === 'tablet' || compactPane === 'chat' || compactPane === 'setup') && (
            <div className="compact-side">
              {renderRight(false, compactPane === 'setup' ? 'inspector' : 'chat')}
            </div>
          )}
          {layout === 'phone' && compactPane === 'nodes' && (
            <div className="compact-side compact-full">{renderSidebar(false)}</div>
          )}
          {layout === 'tablet' && libraryOpen && (
            <div className="compact-drawer-backdrop" onClick={closeLibrary}>
              <div className="compact-drawer" onClick={(e) => e.stopPropagation()}>
                {renderSidebar(false)}
              </div>
            </div>
          )}
          <nav className="compact-bar">
            <button
              type="button"
              className={compactPane === 'chat' ? 'primary' : ''}
              onClick={() => showCompact('chat')}
            >
              Chat
            </button>
            {layout === 'phone' && (
              <button
                type="button"
                className={compactPane === 'canvas' ? 'primary' : ''}
                onClick={() => showCompact('canvas')}
              >
                Canvas
              </button>
            )}
            <button
              type="button"
              className={compactPane === 'setup' ? 'primary' : ''}
              onClick={() => showCompact('setup')}
            >
              {selectedNode ? 'Node' : 'Setup'}
            </button>
            <button
              type="button"
              className={(layout === 'phone' ? compactPane === 'nodes' : libraryOpen) ? 'primary' : ''}
              onClick={toggleLibrary}
            >
              Nodes
            </button>
          </nav>
        </div>
      )}
      <input
        ref={importInputRef}
        type="file"
        accept=".zip,.json,.py"
        hidden
        onChange={(e) => {
          const file = e.target.files?.[0];
          e.target.value = ''; // let the same file be picked again
          onImportFile(file);
        }}
      />
      {confirmState && (
        <div className="modal-backdrop" onClick={() => resolveConfirm(false)}>
          <div className="modal confirm-modal" onClick={(e) => e.stopPropagation()}>
            <h3>{confirmState.title}</h3>
            <p className="muted">{confirmState.body}</p>
            <div className="modal-actions">
              <button type="button" onClick={() => resolveConfirm(false)}>
                Cancel
              </button>
              <button
                type="button"
                className={confirmState.danger ? 'danger' : 'primary'}
                onClick={() => resolveConfirm(true)}
              >
                {confirmState.confirmLabel || 'OK'}
              </button>
            </div>
          </div>
        </div>
      )}
      {drawerOpen && (batch || (run && (run.status === 'success' || run.status === 'failed'))) && (
        <div className="drawer">
          <div className="drawer-head">
            <div className="run-mode-tabs">
              {batch ? (
                <button type="button" className={drawerTab === 'items' ? 'primary' : ''} onClick={() => setDrawerTab('items')}>
                  Records
                </button>
              ) : (
                <>
                  <button type="button" className={drawerTab === 'result' ? 'primary' : ''} onClick={() => setDrawerTab('result')}>
                    Result
                  </button>
                  <button type="button" className={drawerTab === 'nodes' ? 'primary' : ''} onClick={() => setDrawerTab('nodes')}>
                    Nodes
                  </button>
                </>
              )}
              <button type="button" className={drawerTab === 'memory' ? 'primary' : ''} onClick={() => setDrawerTab('memory')}>
                Memory
              </button>
            </div>
            <span className="muted small run-artifacts">
              {batch ? (
                <>
                  <span className={`node-status status-${batch.status === 'completed' ? 'completed' : 'running'}`}>
                    {batch.status}
                  </span>
                  {` · ${batchProgress.done}/${batchProgress.total} done`}
                  {batchProgress.failed > 0 && ` · ${batchProgress.failed} failed`}
                  {batch.metric && ` · metric ${batch.metric}`}
                  {` · artifacts → ${graph?.output_dir || 'runs'}/<run_id>`}
                </>
              ) : (
                <>
                  <span className={`node-status status-${run.status === 'success' ? 'completed' : 'failed'}`}>
                    {run.status}
                  </span>
                  {` · artifacts → ${graph?.output_dir || 'runs'}/${run.run_id}`}
                </>
              )}
            </span>
            <button
              type="button"
              title="Open the artifacts folder"
              onClick={openWorkspace}
            >
              Files
            </button>
            {!batch && (
              <button type="button" onClick={() => copyText(JSON.stringify(run.result, null, 2))}>
                {copied ? 'Copied' : 'Copy'}
              </button>
            )}
            {batch && (
              // A plain link, so the browser saves the file itself: the results
              // can be large, and routing them through JS only to re-serialise
              // them would be pointless.
              <>
                <a
                  className="drawer-download"
                  href={api.batchExportUrl(batch.batch_id, 'csv')}
                  download
                  title="Download every record, its output and its score"
                >
                  CSV
                </a>
                <a
                  className="drawer-download"
                  href={api.batchExportUrl(batch.batch_id, 'json')}
                  download
                  title="Download the results with the metric and aggregate alongside"
                >
                  JSON
                </a>
              </>
            )}
            <button onClick={() => setDrawerOpen(false)}>✕</button>
          </div>
          {drawerTab === 'items' ? (
            <div className="drawer-body">
              {batch?.summary && (
                <div className="eval-summary">
                  <div className="eval-headline">
                    <span className="eval-mean">{batch.summary.mean ?? '—'}</span>
                    <span className="muted small">mean score · {batch.metric}</span>
                  </div>
                  <div className="muted small">
                    {batch.summary.scored}/{batch.summary.total} scored
                    {batch.summary.unscored > 0 && ` · ${batch.summary.unscored} unscored`}
                    {batch.summary.mean != null &&
                      ` · range ${batch.summary.min}–${batch.summary.max}`}
                    {` · ${batch.summary.perfect} perfect`}
                  </div>
                </div>
              )}
              {(batch?.items || []).length === 0 && (
                <p className="muted small">Waiting for the first record…</p>
              )}
              {(batch?.items || []).map((it) => (
                <div className="batch-item" key={it.index}>
                  <div className="batch-item-head">
                    <span>#{it.index + 1}</span>
                    <span>
                      {it.review_status === 'awaiting_review' && (
                        <span className="node-status status-running">awaiting_review </span>
                      )}
                      {it.review_status && it.review_status !== 'awaiting_review' && (
                        <span className="muted small">{it.review_status}→{it.final_action} </span>
                      )}
                      <span className={`node-status status-${it.status === 'success' ? 'completed' : it.status}`}>
                        {it.status}
                      </span>
                    </span>
                  </div>
                  {(it.score != null || it.score_detail) && (
                    <div className="eval-row">
                      {it.score != null && (
                        <span className={`eval-score ${it.score >= 1 ? 'good' : it.score > 0 ? 'partial' : 'bad'}`}>
                          {it.score}
                        </span>
                      )}
                      {it.label != null && (
                        <span className="muted small">
                          expected: {String(typeof it.label === 'string' ? it.label : JSON.stringify(it.label)).slice(0, 80)}
                        </span>
                      )}
                      {it.score_detail?.error && (
                        <span className="chat-error">{it.score_detail.error}</span>
                      )}
                      {it.score_detail && !it.score_detail.error && (
                        <span className="muted small">
                          {Object.entries(it.score_detail).map(([k, v]) => `${k}=${v}`).join(' · ')}
                        </span>
                      )}
                    </div>
                  )}
                  {it.error && <pre className="json-view run-error">{String(it.error)}</pre>}
                  {it.output_summary && <pre className="json-view batch-output">{it.output_summary}</pre>}
                </div>
              ))}
            </div>
          ) : drawerTab === 'result' ? (
            <div className="drawer-body">
              {/* A failed run's error is usually a traceback: it needs the full
                  width and its own scroll, not a line in the header. */}
              {!run && <p className="muted small">Open a single run to see its result.</p>}
              {run?.error && <pre className="json-view run-error">{String(run.error)}</pre>}
              {run?.result != null && (
                <pre className="json-view">{JSON.stringify(run.result, null, 2)}</pre>
              )}
              {run && !run.error && run.result == null && (
                <p className="muted small">This run produced no result.</p>
              )}
            </div>
          ) : drawerTab === 'nodes' ? (
            <div className="drawer-body">
              {(run?.nodes || []).length === 0 && <p className="muted small">No node records.</p>}
              {(run?.nodes || []).map((n) => (
                <div className="run-node" key={n.name}>
                  <div className="run-node-head">
                    <strong>{n.name}</strong>
                    <span className={`node-status status-${n.status}`}>{n.status}</span>
                  </div>
                  {n.error && <pre className="json-view run-error">{String(n.error)}</pre>}
                  {n.output != null && (
                    <pre className="json-view">
                      {typeof n.output === 'string' ? n.output : JSON.stringify(n.output, null, 2)}
                    </pre>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <MemoryPanel graphId={graph?.id} />
          )}
        </div>
      )}
      <RunDialog
        open={activeOverlay === 'run'}
        graphId={graph?.id}
        workflowInputs={workflowInputs}
        hasCanvasSource={hasCanvasSource}
        submitting={runStarting}
        onCancel={closeOverlay}
        onSubmit={startRun}
        beforeRun={saveCurrent}
        onBatchStart={onBatchStart}
      />
      <EvolvePanel
        open={activeOverlay === 'evolve'}
        graphId={graph?.id}
        onClose={closeOverlay}
        onApplied={(g) => {
          refreshList().then(() => openGraph(g.id));
        }}
      />
      <ReviewPanel open={activeOverlay === 'review'} onClose={closeOverlay} />
      <SchedulePanel
        open={activeOverlay === 'schedule'}
        graphId={graph?.id}
        onClose={closeOverlay}
      />
      <RunsPanel
        open={activeOverlay === 'runs'}
        graphId={graph?.id}
        onClose={closeOverlay}
        onOpenRun={openPastRun}
        onOpenBatch={openPastBatch}
      />
    </div>
  );
}

export default function App() {
  return (
    <ReactFlowProvider>
      <Studio />
    </ReactFlowProvider>
  );
}
