import TokenUsage from './components/TokenUsage.jsx';
import { compactTokens, tokenSuffix } from './components/tokenUsageText.js';
import ResizableDrawer from './components/ResizableDrawer.jsx';
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
import { resourceGeometry } from './features/canvas/canvasHandles.js';
import { api } from './api.js';
import { Group, Panel, Separator, useDefaultLayout } from 'react-resizable-panels';
import ChatAgentNode from './features/agents/ChatAgentNode.jsx';
import ResourceMenu from './components/ResourceMenu.jsx';
import ChatAgentInspector from './features/agents/ChatAgentInspector.jsx';
import { useCanvasAgents, isChatNode, chatNodeId, memoryId, memoryBinding } from './features/agents/useCanvasAgents.js';
import MemoryResourceInspector from './features/memory/MemoryResourceInspector.jsx';
import { connectMemory, disconnectMemory, removeMemoryReferences, renameMemoryReferences } from './features/memory/memoryConnections.js';
import { connectEdge, graphToFlow, flowToGraph, uniqueName, memoryOverlay, isMemoryId } from './features/canvas/convert.js';
import { useLayoutMode } from './useLayoutMode.js';
import { BATCH_SETTLED, useExecutionSession } from './features/execution/useExecutionSession.js';
import { RUN_SETTLED, describeBatch, describeRun, isSettled, toneClass } from './features/execution/runStates.js';
import RunOutcome from './features/execution/RunOutcome.jsx';
import UsageTab, { usageVersion } from './features/execution/UsageTab.jsx';
import EvaluationTab from './features/evaluation/EvaluationTab.jsx';
import { resumeLabel } from './features/execution/batchControl.js';
import { batchNodeStates, stageText } from './features/execution/batchStage.js';
import { flowEdges, ioSummary, nodeFocus } from './features/canvas/runFlow.js';
import JsonView from './components/JsonView.jsx';
import { useStudioNavigation } from './useStudioNavigation.js';
import TaskNode from './features/canvas/TaskNode.jsx';
import SourceNode from './features/canvas/SourceNode.jsx';
import ToolNode from './features/canvas/ToolNode.jsx';
import Palette from './features/library/Palette.jsx';
import ToolsPanel from './features/library/ToolsPanel.jsx';
import Inspector from './features/canvas/Inspector.jsx';
import ConnectionEditor from './features/canvas/ConnectionEditor.jsx';
import { memorySiblings } from './features/memory/MemorySettings.jsx';
import RunDialog from './features/execution/RunDialog.jsx';
import MemoryNode from './features/memory/MemoryNode.jsx';
import { RESET_MEMORY_KEYS, describeMemoryAction, isResetMemoryShortcut } from './shortcuts.js';
import MemoryPanel from './features/memory/MemoryPanel.jsx';
import EvolvePanel from './features/evaluation/EvolvePanel.jsx';
import ReviewPanel from './components/ReviewPanel.jsx';
import WorkspacePanel from './features/workspace/WorkspacePanel.jsx';
import ChatPanel, { renameChatHistory } from './features/chat/ChatPanel.jsx';
import RunsPanel from './features/execution/RunsPanel.jsx';
import SchedulePanel from './features/execution/SchedulePanel.jsx';
import TopBar from './components/TopBar.jsx';

const nodeTypes = { task: TaskNode, source: SourceNode, tool: ToolNode, memory: MemoryNode, chatAgent: ChatAgentNode };

// Wide enough that the Library / Custom / Workspace tabs and the collapse chevron
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



// Stable ids for the three main panels. Kept module-level so the array identity
// never changes: `useDefaultLayout` memoises on it, and the ids are part of the
// localStorage key, so a new identity (or a renamed panel) would silently drop
// the saved layout.
const MAIN_PANEL_IDS = ['sidebar', 'canvas', 'inspector'];

export function Studio({ initialGraphId, onHome, projectId, initialRun, initialBatchId } = {}) {
  const [palette, setPalette] = useState([]);
  const [sourcePalette, setSourcePalette] = useState([]);
  const [graphs, setGraphs] = useState([]);
  const [graph, setGraph] = useState(null); // {id, name, goal}
  const graphRef = useRef(graph);
  graphRef.current = graph;
  const reportAgentError = useCallback(e => setErrors(extractErrors(e)), []);
  const canvasAgents = useCanvasAgents(graph?.id, reportAgentError);
  
  const [spaceNames, setSpaceNames] = useState({});
  useEffect(() => {
    let current = true;
    setSpaceNames({});
    if (graph?.id && api.mem0Spaces) api.mem0Spaces(graph.id).then(result => {
      if (current) setSpaceNames(Object.fromEntries(result.spaces.map(s => [s.id, s.name])));
    }).catch(() => {});
    return () => { current = false; };
  }, [graph?.id]);
  const [nodes, setNodes] = useNodesState([]);
  // The current nodes, for callbacks that must not re-create on every change.
  const nodesRef = useRef(nodes);
  nodesRef.current = nodes;
  const [edges, setEdges] = useEdgesState([]);
  // Edges the migration drew for dependencies the old engine implied. Real,
  // and kept in the graph, but a canvas is easier to read without them.
  const [showImplied, setShowImplied] = useState(() => {
    try { return window.localStorage.getItem('evoagentx-studio:show-implied') === '1'; }
    catch { return false; }
  });
  const onToggleImplied = useCallback(() => setShowImplied((v) => {
    try { window.localStorage.setItem('evoagentx-studio:show-implied', v ? '0' : '1'); }
    catch { /* per-viewer convenience only */ }
    return !v;
  }), []);
  // Memory on the canvas: each remembering node's store, the write into it
  // and the reads out of it. Derived from the nodes' settings, never saved.
  const [showMemory, setShowMemory] = useState(() => {
    try { return window.localStorage.getItem('evoagentx-studio:show-memory') !== '0'; }
    catch { return true; }
  });
  const onToggleMemory = useCallback(() => setShowMemory((v) => {
    try { window.localStorage.setItem('evoagentx-studio:show-memory', v ? '0' : '1'); }
    catch { /* per-viewer convenience only */ }
    return !v;
  }), []);
  const [canvasMenu, setCanvasMenu] = useState(null);
  const [selectedMemoryEdges, setSelectedMemoryEdges] = useState(() => new Set());
  const displayEdges = useMemo(() => {
    const flow = showImplied ? edges : edges.filter((e) => !e.data?.implied);
    return showMemory ? [...flow, ...memoryOverlay(nodes, { resources: graph?.memory_resources || [], positions: graph?.memory_positions || {}, spaceNames }).memEdges.map(e => ({ ...e, selected: selectedMemoryEdges.has(e.id) }))] : flow;
  }, [edges, nodes, graph?.memory_resources, graph?.memory_positions, showImplied, showMemory, selectedMemoryEdges, spaceNames]);
  const [selectedId, setSelectedId] = useState(null);
  const [inspectorParent, setInspectorParent] = useState(null);
  useEffect(() => { setInspectorParent(null); }, [graph?.id]);
  const [resourceMeasurements, setResourceMeasurements] = useState({});
  const [memoryFocus, setMemoryFocus] = useState(null);
  const [locatingMemories, setLocatingMemories] = useState(false);
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
    openChat,
    openLeft,
    openRight,
    openInspector,
    openWorkspace,
    revealSelection,
    rightTab,
    showCompact,
    toggleLibrary,
  } = useStudioNavigation(layout);
  const [dirty, setDirty] = useState(false);
  const [editingConnection, setEditingConnection] = useState(null);
  useEffect(() => setEditingConnection(null), [graph?.id]);
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
  const { screenToFlowPosition, setCenter, fitView } = useReactFlow();
  const idRef = useRef(0);
  const leftPanelRef = useRef(null);
  const rightPanelRef = useRef(null);
  const [leftCollapsed, setLeftCollapsed] = useState(false);
  const [rightCollapsed, setRightCollapsed] = useState(false);

  const reportExecutionError = useCallback((error) => {
    setErrors(Array.isArray(error) ? error : extractErrors(error));
  }, []);
  const clearSelection = useCallback(() => { setSelectedId(null); setInspectorParent(null); }, []);
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
    resumeBatch,
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
    runStopRequested,
    setDrawerOpen,
    setDrawerTab,
    unattended,
    unattendedStopping,
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

  const historyTimer = useRef(null);
  const snapshot = (meta, n, e) => ({ scope: meta?.id, nodes: n, edges: e, agents: canvasAgents.loaded ? canvasAgents.agents : undefined, meta: {
    name: meta?.name, goal: meta?.goal, output_dir: meta?.output_dir,
    preprocess: meta?.preprocess, memory_resources: meta?.memory_resources || [],
    memory_positions: meta?.memory_positions || {},
  } });
  const fingerprint = value => JSON.stringify({graph: flowToGraph(value.meta, value.nodes, value.edges), agents: value.agents});
  const currentSnapshot = useRef(null);
  currentSnapshot.current = snapshot(graph, nodes, edges);
  const resetHistory = useCallback((n, e, meta) => {
    clearTimeout(historyTimer.current);
    history.current = { past: [], future: [], last: snapshot(meta, n, e), suppress: false };
  }, []);

  const markClean = useCallback((meta, n, e) => {
    cleanRef.current = JSON.stringify(flowToGraph(meta, n, e));
    setDirty(false);
  }, []);

  useEffect(() => {
    const h = history.current;
    const current = currentSnapshot.current;
    if (h.suppress || !h.last) {
      h.suppress = false;
      h.last = current;
      return undefined;
    }
    if (h.last.agents === undefined && current.agents !== undefined) h.last = {...h.last, agents: current.agents};
    if (fingerprint(h.last) === fingerprint(current)) return undefined;
    historyTimer.current = setTimeout(() => {
      h.past.push(h.last);
      if (h.past.length > HISTORY_LIMIT) h.past.shift();
      h.future = [];
      h.last = current;
    }, SETTLE_MS);
    return () => clearTimeout(historyTimer.current);
  }, [nodes, edges, graph, canvasAgents.agents, canvasAgents.loaded]);

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

  const restoringCanvas = useRef(false);
  const restore = useCallback(
    async (from, to) => {
      if (restoringCanvas.current) return;
      const h = history.current;
      clearTimeout(historyTimer.current);
      // Flush an edit immediately: undo must work before the debounce expires.
      const current = currentSnapshot.current;
      if (h.last && fingerprint(current) !== fingerprint(h.last)) {
        h.past.push(h.last);
        h.future = [];
        h.last = current;
      }
      if (!h[from].length) return;
      const saved = h[from][h[from].length - 1];
      if (saved.agents !== undefined && JSON.stringify(saved.agents) !== JSON.stringify(current.agents)) {
        restoringCanvas.current = true;
        try { await canvasAgents.restore(saved.agents); }
        catch (error) { setErrors(extractErrors(error)); return; }
        finally { restoringCanvas.current = false; }
      }
      if (graphRef.current?.id !== current.scope) return;
      h[to].push(h.last);
      h[from].pop();
      h.suppress = true;
      h.last = saved;
      setNodes(saved.nodes);
      setEdges(saved.edges);
      setGraph(g => ({ ...g, ...saved.meta }));
      setEditingConnection(null);
      setSelectedId(null);
    },
    [setNodes, setEdges, canvasAgents.restore]
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
                     preprocess: g.preprocess || null,
                     memory_resources: g.memory_resources || [], memory_positions: g.memory_positions || {} };
      setResourceMeasurements({});
      setGraph(meta);
      setNodes(n);
      setEdges(e);
      resetHistory(n, e, meta);
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
    const list = projectId ? await api.projectTasks(projectId) : await api.listGraphs();
    const sorted = [...list].sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')));
    setGraphs(sorted);
    return sorted;
  }, [projectId]);

  const newGraph = useCallback(async () => {
    const g = await api.createGraph('Untitled Workflow', '');
    if (projectId && projectId !== 'unassigned') await api.assignTask(projectId, g.id);
    await refreshList();
    await openGraph(g.id);
  }, [refreshList, openGraph, projectId]);

  // ---- template loading ----
  const loadTemplate = useCallback(
    async (templateId) => {
      if (!(await confirmDiscard('Loading a template'))) return;
      const t = await api.getTemplate(templateId);
      const g = await api.createGraph(t.graph.name, t.graph.goal);
      const saved = await api.saveGraph(g.id, t.graph);
      if (projectId && projectId !== 'unassigned') await api.assignTask(projectId, saved.id);
      await refreshList();
      await openGraph(saved.id);
    },
    [refreshList, openGraph, confirmDiscard, projectId]
  );

  useEffect(() => {
    if (initializedRef.current) return;
    initializedRef.current = true;
    (async () => {
      try {
        const list = await refreshList();
        if (list.length) {
          await openGraph(initialGraphId && list.some(g => g.id === initialGraphId) ? initialGraphId : list[0].id);
        } else {
          await newGraph();
        }
        if (initialBatchId) await openPastBatch({batch_id:initialBatchId});
        else if (initialRun) setActiveOverlay('run');
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
      setGraph(g => ({ ...g, memory_resources: memoryOverlay(nodesRef.current, { resources: g?.memory_resources || [] }).memNodes.filter(n => n.data.kind === 'mem0').map(n => ({ space_id: n.data.space_id, name: n.data.title, x: n.position.x, y: n.position.y })) }));
      setNodes((nds) => removeMemoryReferences(nds, [id]));
      setEdges((eds) => eds.filter((e) => e.source !== id && e.target !== id));
      setSelectedId((sel) => (sel === id ? null : sel));
    },
    [setNodes, setEdges]
  );

  const onNodesChange = useCallback(
    (changes) => {
      const dimensions = changes.filter(c => (isMemoryId(c.id) || isChatNode(c.id)) && c.type === 'dimensions' && c.dimensions);
      if (dimensions.length) setResourceMeasurements(previous => {
        let next = previous;
        for (const c of dimensions) {
          if (previous[c.id]?.width !== c.dimensions.width || previous[c.id]?.height !== c.dimensions.height) {
            if (next === previous) next = { ...previous };
            next[c.id] = c.dimensions;
          }
        }
        return next;
      });
      const positions = changes.filter(c => isMemoryId(c.id) && c.type === 'position' && c.position);
      if (positions.length) setGraph(g => ({ ...g, memory_positions: { ...(g.memory_positions || {}), ...Object.fromEntries(positions.map(c => [c.id, c.position])) } }));
      const chatChanges = changes.filter(c => isChatNode(c.id) && c.type === 'position' && c.position);
      if (chatChanges.length) canvasAgents.setAgents(list => list.map(a => {
        const change = chatChanges.find(c => c.id === chatNodeId(a.id) && c.type === 'position' && c.position);
        return change ? { ...a, ...change.position } : a;
      }));
      changes = changes.filter(c => !isMemoryId(c.id) && !isChatNode(c.id));
      if (!changes.length) return;
      const removed = changes.filter((c) => c.type === 'remove').map((c) => c.id);
      if (removed.length) {
        setEdges((eds) => eds.filter((e) => !removed.includes(e.source) && !removed.includes(e.target)));
        setSelectedId((sel) => (removed.includes(sel) ? null : sel));
      }
      if (removed.length) setGraph(g => ({ ...g, memory_resources: memoryOverlay(nodesRef.current, { resources: g?.memory_resources || [] }).memNodes.filter(n => n.data.kind === 'mem0').map(n => ({ space_id: n.data.space_id, name: n.data.title, x: n.position.x, y: n.position.y })) }));
      setNodes((nds) => applyNodeChanges(changes, removeMemoryReferences(nds, removed)));
    },
    [setNodes, setEdges]
  );

  const keepMemoryResources = useCallback(() => {
    const overlay = memoryOverlay(nodesRef.current, { resources: graph?.memory_resources || [] });
    setGraph(g => ({ ...g, memory_resources: overlay.memNodes.filter(n => n.data.kind === 'mem0').map(n => ({ space_id: n.data.space_id, name: n.data.title, x: n.position.x, y: n.position.y })) }));
  }, [graph?.memory_resources]);

  const onEdgesChange = useCallback((changes) => {
    const overlay = memoryOverlay(nodesRef.current, { resources: graph?.memory_resources || [] });
    setSelectedMemoryEdges(current => {
      const next = new Set(current);
      changes.forEach(c => { if (c.type === 'select' && overlay.memEdges.some(e => e.id === c.id)) { if (c.selected) next.add(c.id); else next.delete(c.id); } else if (c.type === 'remove') next.delete(c.id); });
      return next;
    });
    const removed = changes.filter(c => c.type === 'remove').map(c => overlay.memEdges.find(e => e.id === c.id)).filter(Boolean);
    if (removed.length) {
      keepMemoryResources();
      setNodes(nds => removed.reduce((current, edge) => disconnectMemory(current, edge), nds));
    }
    const flowChanges = changes.filter(c => !overlay.memEdges.some(e => e.id === c.id));
    if (flowChanges.length) setEdges(eds => applyEdgeChanges(flowChanges, eds));
  }, [setEdges, setNodes, graph?.memory_resources, keepMemoryResources]);

  const updateNode = useCallback(
    (id, patch) => {
      if ('memory' in patch || 'use_long_term_memory' in patch) setGraph(g => ({ ...g, memory_resources: memoryOverlay(nodesRef.current, { resources: g?.memory_resources || [] }).memNodes.filter(n => n.data.kind === 'mem0').map(n => ({ space_id: n.data.space_id, name: n.data.title, x: n.position.x, y: n.position.y })) }));
      setNodes((nds) => nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, ...patch } } : n)));
    },
    [setNodes]
  );

  const onConnect = useCallback(
    (conn) => {
      if (!conn.source || !conn.target || conn.source === conn.target) return;
      if (isMemoryId(conn.source) || isMemoryId(conn.target)) {
        if (isMemoryId(conn.source) && isMemoryId(conn.target)) { setErrors(['Connect memory to an Agent, not to another memory resource.']); return; }
        const reading = isMemoryId(conn.source);
        const resourceId = reading ? conn.source : conn.target;
        const agentId = reading ? conn.target : conn.source;
        const overlay = memoryOverlay(nodesRef.current, { resources: graph?.memory_resources || [] });
        try {
          const updated = connectMemory(nodesRef.current, overlay.memNodes.find(n => n.id === resourceId), agentId, reading ? 'read' : 'write', conn);
          keepMemoryResources(); setNodes(updated); setErrors(null);
        } catch (e) { setErrors([e.message]); }
        return;
      }
      const id = `e:${conn.source}->${conn.target}`;
      const existing = edges.find(e => e.id === id);
      const edge = existing || connectEdge(nodesRef.current, conn.source, conn.target);
      const conflicts = (edge.data?.mappings || []).some(m => edges.some(other =>
        other.id !== id && other.target === edge.target && !other.data?.control_only
        && other.data?.mappings?.some(binding => binding.to === m.to)));
      if (existing || !edge.data.mappings.length || conflicts) {
        setEditingConnection({ edge, graphId: graph?.id });
      } else {
        setEdges(eds => [...eds, edge]);
        setErrors(null);
      }
    },
    [setEdges, setNodes, edges, graph?.id, graph?.memory_resources, keepMemoryResources]
  );

  // Inspector navigation is driven only by user navigation, never React Flow's
  // asynchronous selection notifications (including notifications from old renders).
  const openNodeInspector = useCallback((_event, node) => {
    setInspectorParent(null);
    setSelectedId(node.id);
    openInspector();
    if (layout === 'desktop') expandPanel(rightPanelRef, 'inspector', DEFAULT_INSPECTOR_SIZE);
  }, [openInspector, layout, expandPanel]);
  const returnToWorkflow = () => {
    setInspectorParent(null);
    setSelectedId(null);
    setSelectedMemoryEdges(new Set());
    setNodes(nds => nds.map(n => ({ ...n, selected: false })));
  };
  const creatingMemoryRef = useRef(false);
  const [creatingMemory, setCreatingMemory] = useState(false);
  const addSharedMemory = async () => {
    if (!graph?.id || creatingMemoryRef.current) return;
    creatingMemoryRef.current = true;
    setCreatingMemory(true);
    const origin = graph.id;
    try {
      const name = uniqueName('Shared memory', new Set((graph.memory_resources || []).map(r => r.name)));
      const space = await api.createMem0Space(origin, name);
      if (graphRef.current?.id !== origin) return;
      const offset = (graphRef.current.memory_resources || []).length * 40;
      setGraph(g => ({ ...g, memory_resources: [...(g.memory_resources || []), { space_id: space.id, name: space.name, x: 360 + offset, y: 100 + offset }] }));
      setSpaceNames(names => ({ ...names, [space.id]: space.name }));
      setShowMemory(true);
      setInspectorParent(null);
      setSelectedId(`mem:space:${space.id}`);
      setMemoryFocus(`mem:space:${space.id}`);
      openInspector();
      if (layout === 'desktop') expandPanel(rightPanelRef, 'inspector', DEFAULT_INSPECTOR_SIZE);
    } catch (e) { setErrors(extractErrors(e)); }
    finally { creatingMemoryRef.current = false; setCreatingMemory(false); }
  };

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
                source: { ...(defaults.source || {}), ...(defaults.source?.type === 'dataloader' ? {loader:'python', read_batch_size:100} : {}) },
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
  const renameNode = useCallback(
    (id, rawName) => {
      const taken = new Set(nodes.map((n) => n.id));
      taken.delete(id);
      const next = uniqueName(rawName, taken);
      setNodes((nds) =>
        renameMemoryReferences(nds.map((n) => {
          if (n.id !== id) return n;
          const { editName, ...rest } = n.data;
          return { ...n, id: next, data: rest };
        }), id, next)
      );
      if (next !== id) {
        // Memory resource IDs are stable; Chat bindings and layout stay put.
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
      setResourceMeasurements({});
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
    // Renaming a workflow renames its identity too, so the id that comes back
    // may not be the one that went out. Everything downstream — run history,
    // the workspace, exports — is keyed on it, so follow it.
    if (saved.id !== graph.id) renameChatHistory(graph.id, saved.id);
    const fromServer = { id: saved.id, name: saved.name, goal: saved.goal || '',
                         output_dir: saved.output_dir || 'runs',
                         preprocess: saved.preprocess || null };
    const meta = { ...graph, ...fromServer };
    // Merge onto the settings as they are now, not as they were sent: an edit
    // made while the save was in flight must survive it (and stay unsaved).
    setGraph((current) => {
      if (!current || current.id !== graph.id) return current;
      const merged = { ...current, id: saved.id };
      for (const key of ['name', 'goal', 'output_dir', 'preprocess']) {
        if (current[key] === graph[key]) merged[key] = fromServer[key];
      }
      return merged;
    });
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

  // Two separate steps. Backing up takes nothing away and is allowed at any
  // time; clearing is refused while anything runs, and keeps a copy first
  // unless the person says otherwise.
  const backupMemory = useCallback(async () => {
    if (!graph?.id) return;
    try {
      setErrors([describeMemoryAction(await api.backupMemory(graph.id))]);
    } catch (err) {
      setErrors(extractErrors(err));
    }
  }, [graph?.id]);
  const resetMemory = useCallback(async () => {
    if (!graph?.id) return;
    try {
      const out = await api.resetMemory(graph.id);
      setErrors([describeMemoryAction(out)]);
    } catch (err) {
      setErrors(extractErrors(err));
    }
  }, [graph?.id]);
  const resetMemoryRef = useRef(resetMemory);
  resetMemoryRef.current = resetMemory;

  // ---- keyboard + unload guard ----
  useEffect(() => {
    const onKey = (e) => {
      const el = e.target;
      // Text fields keep their own native undo stack; never hijack those.
      const typing = el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable);
      const mod = e.metaKey || e.ctrlKey;
      if (!mod) return;
      if (isResetMemoryShortcut(e)) {
        e.preventDefault();
        if (!typing && !runMode) resetMemoryRef.current?.();
        return;
      }
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
        if (projectId && projectId !== 'unassigned') await api.assignTask(projectId, imported.id);
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
    [refreshList, openGraph, confirmDiscard, projectId]
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
      const previousId = graphRef.current?.id;
      if (g.id && previousId && g.id !== previousId) renameChatHistory(previousId, g.id);
      const { nodes: n, edges: e } = graphToFlow(g);
      setNodes(n);
      setEdges(e);
      setGraph((prev) => ({
        ...prev,
        id: g.id ?? prev.id,
        task_id: g.task_id ?? prev.task_id,
        name: g.name ?? prev.name,
        goal: g.goal ?? prev.goal,
        output_dir: g.output_dir ?? prev.output_dir,
        preprocess: Object.hasOwn(g, 'preprocess') ? g.preprocess : prev.preprocess,
        memory_resources: g.memory_resources ?? prev.memory_resources,
        memory_positions: g.memory_positions ?? prev.memory_positions,
        flow_version: g.flow_version ?? prev.flow_version,
        migration_warnings: g.migration_warnings || [],
      }));
      setSelectedId(null);
      setErrors(null);
    },
    [setNodes, setEdges]
  );

  // ---- run ----
  const startRun = useCallback(
    async (inputs, startAt, session, planId, record) => {
      if (!graph?.id) return { ok: false, error: new Error('No workflow is open.') };
      let startedGraphId;
      const outcome = await launchRun(async () => {
        // Run what you see: persist the canvas before starting the run,
        // otherwise the server executes the last *saved* version.
        const current = await saveCurrent();
        if (!current?.id) return null;
        startedGraphId = current.id;
        const { run_id: runId } = await api.runGraph(
          current.id, inputs, startAt, session, planId, record);
        return { run_id: runId, status: 'running', nodes: [], result: null, error: null };
      });
      // Only a run that started takes the dialog with it: a refusal has to be
      // read where the inputs still are.
      if (outcome.ok) closeOverlay();
      const detail = outcome.error?.body?.detail;
      return outcome.ok ? { ...outcome, graphId: startedGraphId } : {
        ...outcome,
        stale: detail?.code === 'plan_stale',
        error: detail?.message || (Array.isArray(detail) ? detail.join(' ') : detail)
          || outcome.error?.message || String(outcome.error),
      };
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
      // Whether a node takes part is its own flag now, not inferred from
      // whether it happens to have an edge.
      // Node by node the stage names the running node; record by record the
      // per-node aggregate still does. Both live in batchStage.js.
      const batchStates = batchNodeStates(batch, nodes, edges);
      const watchingNodes = new Set((watchInfo?.watchers || []).map((w) => w.node));
      const statuses = nodes.map((n) => {
        if (!runMode) return { runStatus: null, batchBadge: null };
        if (batchStates) {
          return { runStatus: batchStates[n.id]?.runStatus || 'pending',
                   batchBadge: batchStates[n.id]?.batchBadge || null };
        }
        return { runStatus: runByName[n.id]?.status || 'pending',
                 batchBadge: runByName[n.id]?.token_usage?.reported_calls
                   ? compactTokens(runByName[n.id].token_usage) : null };
      });
      // While something runs, it is the focus and the rest steps back.
      const anyRunning = statuses.some((s) => s.runStatus === 'running');
      return nodes.map((n, i) => {
        const { runStatus, batchBadge } = statuses[i];
        return {
          ...n,
          className: n.data.enabled === false ? 'parked' : '',
          data: {
            ...n.data,
            onDelete: deleteNode,
            runMode,
            runStatus,
            batchBadge,
            focus: nodeFocus(runStatus, anyRunning),
            io: runStatus === 'running' ? ioSummary(n.data) : null,
            watching: watchingNodes.has(n.id),
          },
        };
      });
    },
    [nodes, edges, deleteNode, runMode, runByName, batch, watchInfo]
  );
  // Where a node-major batch is, said in node terms, for the badge and the
  // drawer: "node 3/8 · detect · 120/275". Null for a record-major batch.
  const batchStageText = stageText(batch);
  const removeCanvasResource = useCallback(async id => {
    const origin = graph?.id;
    try {
      if (isChatNode(id)) {
        await canvasAgents.remove(id.slice('chat:'.length));
      } else {
        const resource = memoryOverlay(nodesRef.current, { resources: graph?.memory_resources || [] }).memNodes.find(n => n.id === id);
        if (!resource) return;
        for (const agent of canvasAgents.agents.filter(a => a.memories.some(m => memoryId(m) === id))) {
          await canvasAgents.save(agent, current => ({ memories: current.memories.filter(m => memoryId(m) !== id) }));
        }
        if (graphRef.current?.id !== origin) return;
        setNodes(nds => {
          const related = memoryOverlay(nds, { resources: graph?.memory_resources || [] }).memEdges.filter(e => e.data.resource === id);
          return related.reduce((current, edge) => disconnectMemory(current, edge), nds).map(n => {
            const owns = resource.data.kind === 'mem0'
              ? n.data.memory?.provider === 'mem0' && n.data.memory.space_id === resource.data.space_id
              : n.id === resource.data.owner;
            return owns ? { ...n, data: { ...n.data, use_long_term_memory: false,
              memory: { ...n.data.memory, read_enabled: false, write_enabled: false, ...(resource.data.kind === 'mem0' ? { space_id: undefined } : {}) } } } : n;
          });
        });
        setGraph(g => ({ ...g, memory_resources: (g.memory_resources || []).filter(r => r.space_id !== resource.data.space_id) }));
      }
      if (graphRef.current?.id !== origin) return;
      setSelectedId(selected => selected === id ? null : selected);
      setInspectorParent(parent => parent === id ? null : parent);
      setSelectedMemoryEdges(new Set());
    } catch (e) { reportAgentError(e); }
  }, [graph?.id, graph?.memory_resources, canvasAgents.remove, canvasAgents.save, canvasAgents.agents, setNodes, reportAgentError]);
  const memoryNodes = useMemo(() => {
    if (!showMemory) return [];
    const wrote = {};
    (run?.memory_written || []).forEach((w) => { wrote[w.node] = (wrote[w.node] || 0) + 1; });
    return memoryOverlay(nodes, { wrote, resources: graph?.memory_resources || [], positions: graph?.memory_positions || {}, spaceNames }).memNodes.map(n => {
      const connections = canvasAgents.agents.flatMap(a => a.memories.filter(m => memoryId(m) === n.id).map(m => ({ ...m, agent: chatNodeId(a.id) })));
      return { ...resourceGeometry(n, resourceMeasurements[n.id]), data: { ...n.data, readers: [...n.data.readers, ...connections.filter(m => m.read).map(m => m.agent)], writers: [...n.data.writers, ...connections.filter(m => m.write).map(m => m.agent)], onDelete: removeCanvasResource, runMode }, measured: resourceMeasurements[n.id], selected: n.id === selectedId };
    });
  }, [nodes, selectedId, showMemory, spaceNames, resourceMeasurements, canvasAgents.agents, removeCanvasResource, runMode, run?.memory_written, graph?.memory_resources, graph?.memory_positions]);
  useEffect(() => {
    if (!memoryFocus) return;
    const resource = memoryNodes.find(n => n.id === memoryFocus);
    if (!resource) return;
    setCenter(resource.position.x + 130, resource.position.y + 60, { zoom: 0.85, duration: 200 });
    setMemoryFocus(null);
  }, [memoryFocus, memoryNodes, setCenter]);
  useEffect(() => {
    if (!locatingMemories || !memoryNodes.length) return;
    fitView({ nodes: memoryNodes.map(n => ({ id: n.id })), padding: 0.3, duration: 200, maxZoom: 1 });
    setLocatingMemories(false);
  }, [locatingMemories, memoryNodes, fitView]);
  const canvasNodes = useMemo(() => [...displayNodes, ...memoryNodes, ...canvasAgents.nodes.map(n => ({ ...resourceGeometry(n, resourceMeasurements[n.id]), data: { ...n.data, onDelete: removeCanvasResource, runMode }, measured: resourceMeasurements[n.id], selected: n.id === selectedId }))], [displayNodes, memoryNodes, canvasAgents.nodes, selectedId, resourceMeasurements, removeCanvasResource, runMode]);
  const fittedResourceGraph = useRef(null);
  // The opening fit-to-graph has happened: following the running node before
  // it would be undone by it.
  const [fittedGraph, setFittedGraph] = useState(null);
  useEffect(() => {
    if (!graph?.id || !canvasAgents.loaded || fittedResourceGraph.current === graph.id) return undefined;
    const frame = requestAnimationFrame(() => {
      fitView({padding:0.2, maxZoom:1});
      fittedResourceGraph.current = graph.id;
      setFittedGraph(graph.id);
    });
    return () => cancelAnimationFrame(frame);
  }, [graph?.id, canvasAgents.loaded, canvasNodes, fitView]);

  // The camera follows the node that is working now, so it is always in
  // view. Moving the canvas by hand stops the following until the next run —
  // a camera that fights the person looking around is worse than none.
  const followRun = useRef(true);
  const runKey = (batch || run)?.batch_id || (batch || run)?.run_id || null;
  useEffect(() => { followRun.current = true; }, [runKey]);
  const runningNode = runMode ? displayNodes.find((n) => n.data.runStatus === 'running') : null;
  const runningId = runningNode?.id || null;
  useEffect(() => {
    if (!runningNode || !followRun.current || fittedGraph !== graph?.id) return;
    const width = runningNode.measured?.width || runningNode.width || 220;
    const height = runningNode.measured?.height || runningNode.height || 120;
    setCenter(runningNode.position.x + width / 2, runningNode.position.y + height / 2,
      { zoom: 1.05, duration: 500 });
    // Only when the running node changes (or the opening fit has settled);
    // its badge ticking is not a move.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runningId, setCenter, fittedGraph]);

  // Runtime decoration only: never persist animation into workflow edges.
  // Input flowing into the running node, its output waiting to leave, what
  // has already been delivered — see features/canvas/runFlow.js.
  const executionEdges = useMemo(() => {
    const active = runMode && ['running', 'cancelling'].includes((batch || run)?.status);
    const byId = new Map(displayNodes.map(node => [node.id, node]));
    const statusOf = (id) => {
      const node = byId.get(id);
      return node && node.data.enabled !== false ? node.data.runStatus : null;
    };
    return flowEdges(displayEdges, statusOf, active);
  }, [displayEdges, displayNodes, runMode, batch, run]);
  const chatAgent = canvasAgents.agents.find(a => chatNodeId(a.id) === selectedId);
  const chatEdges = showMemory ? canvasAgents.edges.filter(e => memoryNodes.some(n => n.id === e.source || n.id === e.target)).map(e => ({ ...e, selected: selectedMemoryEdges.has(e.id), deletable: true })) : [];
  const connectCanvas = conn => {
    if (!isChatNode(conn.source) && !isChatNode(conn.target)) return onConnect(conn);
    const read = isChatNode(conn.target);
    const id = read ? conn.target : conn.source;
    const resourceId = read ? conn.source : conn.target;
    const resource = memoryOverlay(nodesRef.current, { resources: graph?.memory_resources || [] }).memNodes.find(n => n.id === resourceId);
    if (!resource) { setErrors(['Connect Chat to an existing Memory resource.']); return; }
    if (!read && resource.data.kind !== 'mem0') { setErrors(['This history is written by its workflow Agent. Chat can read it; use a shared Memory for Chat writes.']); return; }
    const agent = canvasAgents.agents.find(a => chatNodeId(a.id) === id);
    canvasAgents.save(agent, current => {
      const previous = current.memories.find(m => memoryId(m) === resourceId) || { ...memoryBinding(resourceId), read: false, write: false };
      return { memories: [...current.memories.filter(m => memoryId(m) !== resourceId), { ...previous, [read ? 'read' : 'write']: true, [read ? 'read_source_handle' : 'write_source_handle']: conn.sourceHandle || (read ? 's-out' : 'out'), [read ? 'read_target_handle' : 'write_target_handle']: conn.targetHandle || (read ? 'in' : 's-in') }] };
    }).catch(reportAgentError);
  };
  const changeCanvasEdges = changes => {
    setSelectedMemoryEdges(current => {
      const next = new Set(current);
      changes.filter(c => chatEdges.some(e => e.id === c.id)).forEach(c => {
        if (c.type === 'select' && c.selected) next.add(c.id);
        else if (c.type === 'select' || c.type === 'remove') next.delete(c.id);
      });
      return next;
    });
    for (const change of changes) {
      const edge = chatEdges.find(e => e.id === change.id);
      if (edge && change.type === 'remove') {
        const agent = canvasAgents.agents.find(a => a.id === edge.data.chatAgent);
        canvasAgents.save(agent, current => ({ memories: current.memories.map(m => memoryId(m) === edge.data.space ? { ...m, [edge.data.direction]: false } : m).filter(m => m.read || m.write) })).catch(reportAgentError);
      }
    }
    onEdgesChange(changes.filter(c => !chatEdges.some(e => e.id === c.id)));
  };

  const selectedMemory = memoryOverlay(nodes, { resources: graph?.memory_resources || [], positions: graph?.memory_positions || {}, spaceNames }).memNodes.find(n => n.id === selectedId);
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
        <button type="button" className={leftTab === 'library' ? 'primary' : ''} onClick={() => openLeft('library')}>
          Library
        </button>
        <button type="button" className={leftTab === 'custom' ? 'primary' : ''} onClick={() => openLeft('custom')}>
          Custom
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
        {leftTab === 'library' && (<>
          <div className="library-resources">
            <div className="library-heading"><span className="library-eyebrow">BUILD YOUR WORKFLOW</span><h2>Library</h2><p>Everything you need to start building.</p></div>
            <div className="library-resource-actions">
          <button className="library-resource-action library-chat-action" aria-label="+ Add Chat Agent" type="button" disabled={!graph?.id || canvasAgents.busy} onClick={async () => {
            try {
              const origin = graph?.id;
              const agent = await canvasAgents.add();
              if (graphRef.current?.id !== origin) return;
              openNodeInspector(null, { id: chatNodeId(agent.id) });
              setCenter(agent.x + 130, agent.y + 60, { zoom: 0.85, duration: 200 });
            } catch (e) { reportAgentError(e); }
          }}><span className="library-resource-icon" aria-hidden="true">◌</span><span><strong>{canvasAgents.busy ? 'Adding Agent…' : 'Chat Agent'}</strong><small>An independent conversation</small></span><span className="library-add" aria-hidden="true">+</span></button>
          <button className="library-resource-action library-memory-action" aria-label="+ Add shared memory" type="button" disabled={runMode || !graph?.id || creatingMemory} onClick={addSharedMemory}><span className="library-resource-icon" aria-hidden="true">▤</span><span><strong>{creatingMemory ? 'Adding memory…' : 'Shared memory'}</strong><small>Knowledge your Agents can share</small></span><span className="library-add" aria-hidden="true">+</span></button>
          </div>
          {memoryOverlay(nodes, { resources: graph?.memory_resources || [] }).memNodes.length > 0 && <button className="library-locate" type="button" onClick={() => {
            setShowMemory(true);
            try { window.localStorage.setItem('evoagentx-studio:show-memory', '1'); } catch { /* optional preference */ }
            setLocatingMemories(true);
          }}>{showMemory ? 'Locate memories' : 'Show and locate memories'}</button>}
          </div>
          <Palette templates={palette} sources={sourcePalette} graphTemplates={graphTemplates} onAdd={(tpl) => addNode(tpl)} onLoadTemplate={(id) => loadTemplate(id).catch((err) => setErrors(extractErrors(err)))} disabled={runMode} />
        </>)}
        {leftTab === 'custom' && <ToolsPanel onAdd={(tpl) => addNode(tpl)} disabled={runMode} />}
        {leftTab === 'workspace' && (
          <WorkspacePanel open graphId={graph?.id} onClose={() => openLeft('library')} />
        )}
      </div>
    </div>
  );

  const renderCanvas = (collapsible) => (
    <div className="canvas">
      {canvasMenu && <ResourceMenu menu={canvasMenu} onClose={() => setCanvasMenu(null)} items={canvasMenu.node ? [
        { label: 'Open settings', action: () => openNodeInspector(null, canvasMenu.node) },
        { label: 'Remove from canvas', danger: true, disabled: runMode, action: () => {
          const id = canvasMenu.node.id;
          if (isChatNode(id) || isMemoryId(id)) removeCanvasResource(id); else deleteNode(id);
        } },
      ] : [{ label: 'Delete connection', danger: true, disabled: runMode || canvasMenu.edge.deletable === false, action: () => changeCanvasEdges([{ id: canvasMenu.edge.id, type: 'remove' }]) }]} />}

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
      {!runMode && editingConnection?.graphId === graph?.id && editingConnection && <ConnectionEditor
        key={`${graph?.id}:${editingConnection.edge.id}`}
        edge={editingConnection.edge} nodes={nodes} edges={edges}
        onClose={() => setEditingConnection(null)}
        onDelete={edges.some(e => e.id === editingConnection.edge.id) ? () => {
          setEdges(current => current.filter(e => e.id !== editingConnection.edge.id));
          setEditingConnection(null);
        } : undefined}
        onSave={edge => {
          setEdges(current => [...current.filter(e => e.id !== edge.id), edge]);
          setEditingConnection(null); setErrors(null);
        }}
      />}
      <ReactFlow
        nodes={canvasNodes}
        edges={[...executionEdges, ...chatEdges]}
        onMoveStart={(event) => { if (event) followRun.current = false; }}
        nodeTypes={nodeTypes}
        onNodesChange={changes => onNodesChange(runMode ? changes.filter(change => change.type === 'dimensions') : changes)}
        onEdgesChange={runMode ? undefined : changeCanvasEdges}
        onConnect={runMode ? undefined : connectCanvas}
        onNodeContextMenu={(e, node) => { e.preventDefault(); setCanvasMenu({ x: e.clientX, y: e.clientY, task: { name: node.data.title || node.id }, node }); }}
        onEdgeContextMenu={(e, edge) => { e.preventDefault(); setCanvasMenu({ x: e.clientX, y: e.clientY, task: { name: edge.label || 'Connection' }, edge }); }}
        onEdgeClick={(_event, edge) => {
          if (!runMode && edges.some(e => e.id === edge.id))
            setEditingConnection({ edge, graphId: graph?.id });
        }}
        onNodeClick={openNodeInspector}
        onNodeDragStop={(_event, node) => {
          if (isChatNode(node.id)) {
            const agent = canvasAgents.agents.find(a => chatNodeId(a.id) === node.id);
            canvasAgents.save(agent, node.position).catch(reportAgentError);
          }
        }}
        onPaneClick={returnToWorkflow}
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
      {!runMode && nodes.length === 0 && memoryNodes.length === 0 && (
        <div className="canvas-empty">
          <div className="canvas-empty-card">
            <span className="canvas-empty-kicker">New workflow</span>
            <h2>What should this workflow do?</h2>
            <p>Add a task directly, choose a template from the node library, or describe it in Chat.</p>
            <div className="canvas-empty-actions">
              <button type="button" className="primary" onClick={() => addNode(FALLBACK_PALETTE[0])}>
                + Add LLM task
              </button>
              <button type="button" onClick={openChat}>Ask Chat</button>
            </div>
          </div>
        </div>
      )}
      {runMode && (batch || unattended || run) && (
        <div className="run-badge-bar">
        {runMode && batch && (
          <div className="run-badge-group">
            <button
              type="button"
              title="Show the records"
              onClick={() => setDrawerOpen((o) => !o)}
              className={`run-badge ${
                `run-${describeBatch(batch.status, batch.counts || batchProgress).tone}`}`}
            >
              {batchStageText
                // Node by node, no record is finished before the last node,
                // so a record count reads 0/16 until the whole flow is done:
                // the stage is the progress. Records done are in the drawer.
                ? `batch · ${batchStageText}`
                : batchProgress.total
                  ? `batch ${batchProgress.done}/${batchProgress.total}`
                  : `batch ${batch.status}`}
              {` · ${describeBatch(batch.status, batch.counts || batchProgress).label}`}
              {batchProgress.failed > 0 && ` · ${batchProgress.failed}✗`}
              {batch.error && ` · ${batch.error.length > 90 ? `${batch.error.slice(0, 90)}…` : batch.error}`}
              {batch.summary?.mean != null && ` · score ${batch.summary.mean}`}
              {tokenSuffix(batch.token_usage, !isSettled(BATCH_SETTLED, batch.status))}
            </button>
            {!isSettled(BATCH_SETTLED, batch.status) && (
              <button
                type="button"
                className="run-badge run-stop"
                disabled={batch.status === 'cancelling'}
                title="Start no more records and interrupt the ones running, where they are."
                onClick={cancelBatch}
              >
                {batch.status === 'cancelling' ? 'Stopping…' : 'Stop'}
              </button>
            )}
            {isSettled(BATCH_SETTLED, batch.status) && resumeLabel(batch) && (
              <button
                type="button"
                className="run-badge run-resume"
                title="Run the records that did not finish; the finished ones are kept. Uses the memory as it is now — do not reset it first."
                onClick={() => resumeBatch(batch.batch_id)}
              >
                {resumeLabel(batch)}
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
              disabled={unattendedStopping}
              title={unattendedStopping
                ? `Batch ${unattended} was stopped; its records in flight are being interrupted.`
                : `Batch ${unattended} is still running and is not the one shown here.`}
              onClick={() => cancelBatch(unattended)}
            >
              {unattendedStopping
                ? `stopping batch ${unattended.slice(0, 8)}…`
                : `stop batch ${unattended.slice(0, 8)} (still running)`}
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
              {tokenSuffix(run.token_usage, !isSettled(RUN_SETTLED, run.status))}
            </button>
            {!isSettled(RUN_SETTLED, run.status) && (
              // Rendered through the stop too, disabled: the button used to
              // vanish the moment Stop was clicked, so the one thing left on
              // screen was a badge with nothing saying the click was taken.
              <button
                type="button"
                className="run-badge run-stop"
                disabled={runStopRequested}
                title="Stop this run where it is. The model call in flight is abandoned."
                onClick={abandonRun}
              >
                {runStopRequested ? 'Stopping…' : 'Stop run'}
              </button>
            )}
          </div>
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
          chatAgent ? <ChatAgentInspector key={`${graph?.id}:${chatAgent.id}`} graphId={graph?.id} agent={chatAgent} saving={canvasAgents.isSaving(chatAgent.id)} memoryNodes={memoryOverlay(nodes, { resources: graph?.memory_resources || [], spaceNames }).memNodes} backLabel={inspectorParent ? '← Back to memory' : '← Back to workflow'} onBack={() => { if (inspectorParent) { setSelectedId(inspectorParent); setInspectorParent(null); } else returnToWorkflow(); }} onSave={canvasAgents.save} /> :
          isMemoryId(selectedId) ? <MemoryResourceInspector key={`${graph?.id}:${selectedId}`}
            resource={selectedMemory} graphId={graph?.id} nodes={[...nodes, ...canvasAgents.nodes]} edges={[...executionEdges, ...chatEdges]} runMode={runMode}
            onBack={returnToWorkflow}
            onEditAgent={id => { setInspectorParent(selectedId); setSelectedId(id); setNodes(nds => nds.map(n => ({ ...n, selected: n.id === id }))); openInspector(); }}
            onConnect={(agent, direction) => connectCanvas(direction === 'read' ? { source: selectedId, target: agent } : { source: agent, target: selectedId })}
            onDisconnect={edge => changeCanvasEdges([{ id: edge.id, type: 'remove' }])}
            onRemove={() => selectedMemory && removeCanvasResource(selectedMemory.id)}
          /> : <div className="inspector-navigation">
            {selectedNode && <div className="inspector-navigation-header">
              <button type="button" className="link" onClick={() => {
                setSelectedId(inspectorParent);
                setInspectorParent(null);
                setSelectedMemoryEdges(new Set());
                setNodes(nds => nds.map(n => ({ ...n, selected: false })));
                openInspector();
              }}>{inspectorParent ? '← Back to memory' : '← Back to workflow'}</button>
            </div>}
            <Inspector
            getGraph={chatSnapshot}
            node={selectedNode}
            runInfo={selectedNode ? runByName[selectedNode.id] : null}
            runMode={runMode}
            onUpdate={updateNode}
            onRename={renameNode}
            graph={graph}
            onGraphChange={(patch) => setGraph((g) => ({ ...g, ...patch }))}
            producedNames={producedNames}
            siblings={siblings}
          /></div>
        ) : (
          <ChatPanel
            key={graph?.id}
            graphId={graph?.id}
            getGraph={chatSnapshot}
            onApply={applyChatGraph}
            onSaved={() => { pendingCleanRef.current = true; refreshList(); }}
            onOpenPanel={panel => panel === 'workspace' ? openWorkspace() : openOverlay(panel)}
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
      {onHome && <button className="project-return" onClick={async () => { if (await confirmDiscard("Return to projects")) onHome(); }}>← Projects</button>}
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
        showImplied={showImplied}
        onToggleImplied={onToggleImplied}
        showMemory={showMemory}
        onToggleMemory={onToggleMemory}
        onResetMemory={resetMemory}
        onBackupMemory={backupMemory}
        resetMemoryKeys={RESET_MEMORY_KEYS}
        impliedCount={edges.filter((e) => e.data?.implied).length}
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
          {layout === 'phone' && compactPane === 'library' && (
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
              className={(layout === 'phone' ? compactPane === 'library' : libraryOpen) ? 'primary' : ''}
              onClick={toggleLibrary}
            >
              Library
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
      {drawerOpen && (batch || (run && isSettled(RUN_SETTLED, run.status))) && (
        <ResizableDrawer>
          <div className="drawer-head">
            <div className="run-mode-tabs">
              {batch ? (
                <>
                  <button type="button" className={drawerTab === 'items' ? 'primary' : ''} onClick={() => setDrawerTab('items')}>
                    Records
                  </button>
                  <button type="button" className={drawerTab === 'evaluation' ? 'primary' : ''} onClick={() => setDrawerTab('evaluation')}>
                    Evaluation
                  </button>
                </>
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
              <button type="button" className={drawerTab === 'usage' ? 'primary' : ''} onClick={() => setDrawerTab('usage')}>
                Usage
              </button>
            </div>
            <span className="muted small run-artifacts">
              {batch ? (
                <>
                  <span className={`node-status ${toneClass(describeBatch(batch.status, batch.counts || batchProgress).tone)}`}>
                    {describeBatch(batch.status, batch.counts || batchProgress).label}
                  </span>
                  {batchStageText && ` · ${batchStageText}`}
                  {` · ${batchProgress.done}/${batchProgress.total ?? '?'} records done`}
                  {batchProgress.failed > 0 && ` · ${batchProgress.failed} failed`}
                  {batch.metric && ` · metric ${batch.metric}`}
                  {` · logs → ${graph?.output_dir || 'runs'}/<started-at>/nodes/<node>.jsonl`}
                </>
              ) : (
                <>
                  <span className={`node-status ${toneClass(describeRun(run.status).tone)}`}>
                    {describeRun(run.status).label}
                  </span>
                  {` · logs → ${graph?.output_dir || 'runs'}/<started-at>/nodes/<node>.jsonl`}
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
              <TokenUsage usage={batch?.token_usage} running={!!batch && !isSettled(BATCH_SETTLED, batch.status)}/>
              {batch?.streaming && <p className="muted small">{batch.dataset_initialized
                ? `Dataset total: ${batch.dataset_length ?? 'unknown (no __len__)'} · Selected for this run: ${batch.input_total ?? 'unknown'}`
                : 'Initializing Dataset; total count is not available yet.'}</p>}
              {batch?.polling_error && <p role="alert" className="chat-error">{batch.polling_error}</p>}
              {batch?.error && <pre role="alert" className="chat-error">{batch.error}</pre>}
              {batch?.error_detail && <details><summary className="muted small">Where it failed</summary>
                <pre className="json-view run-error">{batch.error_detail}</pre></details>}
              {(batch?.items || []).length === 0 && !batch?.error && (
                <p className="muted small">{isSettled(BATCH_SETTLED, batch.status)
                  ? 'No records were processed.'
                  : batch.streaming
                    ? `Preparing DataLoader / waiting for its first batch (${batch.batch_size || batch.source?.config?.read_batch_size || 100} records per batch). No workflow records have been submitted yet. Check Dataset initialization, preprocessing and file paths if this takes too long.`
                    : 'Waiting for the first record…'}</p>
              )}
              {(batch?.items || []).map((it) => (
                <div className="batch-item" key={it.index}>
                  <div className="batch-item-head">
                    <span>#{it.index + 1}{tokenSuffix(it.token_usage, it.status === 'running')}</span>
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
                  {it.output_summary && <JsonView value={it.output_summary} className="batch-output" />}
                </div>
              ))}
            </div>
          ) : drawerTab === 'evaluation' && batch ? (
            <EvaluationTab batch={batch} />
          ) : drawerTab === 'result' ? (
            <div className="drawer-body">
              {!run && <p className="muted small">Open a single run to see its result.</p>}
              {/* The sentence, the outputs, then the traceback behind a click. */}
              <RunOutcome run={run} />
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
                  {n.token_usage?.reported_calls > 0 && <TokenUsage usage={n.token_usage} />}
                  {n.error && <pre className="json-view run-error">{String(n.error)}</pre>}
                  {n.output != null && <JsonView value={n.output} />}
                </div>
              ))}
            </div>
          ) : drawerTab === 'usage' ? (
            <UsageTab runId={batch ? null : run?.run_id} batchId={batch?.batch_id}
                      live={batch ? batch.status : run?.status}
                      version={usageVersion(batch || run)} />
          ) : (
            <MemoryPanel graphId={graph?.id} />
          )}
        </ResizableDrawer>
      )}
      <RunDialog
        open={activeOverlay === 'run'}
        graphId={graph?.id}
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
        onResumeBatch={(b) => { closeOverlay(); resumeBatch(b.batch_id); }}
      />
    </div>
  );
}

export default function App(props) {
  return (
    <ReactFlowProvider>
      <Studio {...props} />
    </ReactFlowProvider>
  );
}
