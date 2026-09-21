import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import App from './App.jsx';

const state = vi.hoisted(() => ({ layout: 'desktop', center: vi.fn(), fit: vi.fn() }));
const api = vi.hoisted(() => ({
  listTools: vi.fn(async () => ({ tools: [] })),
  listSkills: vi.fn(async () => ({ skills: [] })),
  chatMemoryResources: vi.fn(),
  canvasAgents: vi.fn(),
  restoreCanvasAgents: vi.fn(async (_id, agents) => ({agents})),
  createCanvasAgent: vi.fn(),
  updateCanvasAgent: vi.fn(),
  removeCanvasAgent: vi.fn(),
  agentSessions: vi.fn(),
  createAgentSession: vi.fn(),
  deleteAgentSession: vi.fn(),
  sendAgentMessage: vi.fn(),
  mem0Spaces: vi.fn(),
  createMem0Space: vi.fn(),
  palette: vi.fn(),
  listTemplates: vi.fn(),
  listGraphs: vi.fn(),
  projectTasks: vi.fn(),
  assignTask: vi.fn(),
  getGraph: vi.fn(),
  createGraph: vi.fn(),
  getWatch: vi.fn(),
  listBatches: vi.fn(),
  saveGraph: vi.fn(),
  runGraph: vi.fn(),
  startWatch: vi.fn(),
  importGraph: vi.fn(),
}));

vi.mock('./api.js', () => ({ api }));
vi.mock('./useLayoutMode.js', () => ({ useLayoutMode: () => state.layout }));

vi.mock('@xyflow/react', async () => {
  const ReactModule = await import('react');
  return {
    ReactFlowProvider: ({ children }) => children,
    ReactFlow: (props) => { state.canvas = props; return <div data-testid="canvas">{props.children}</div>; },
    Background: () => null,
    Controls: () => null,
    MiniMap: () => null,
    useNodesState: (initial) => {
      const [value, setValue] = ReactModule.useState(initial);
      return [value, setValue, vi.fn()];
    },
    useEdgesState: (initial) => {
      const [value, setValue] = ReactModule.useState(initial);
      return [value, setValue, vi.fn()];
    },
    useReactFlow: () => ({ screenToFlowPosition: (point) => point, setCenter: state.center, fitView: state.fit }),
    applyNodeChanges: (_changes, nodes) => nodes,
    applyEdgeChanges: (_changes, edges) => edges,
  };
});

vi.mock('react-resizable-panels', () => ({
  Group: ({ children }) => <div>{children}</div>,
  Panel: ({ children }) => <div>{children}</div>,
  Separator: () => null,
  useDefaultLayout: () => ({ defaultLayout: undefined, onLayoutChanged: vi.fn() }),
}));

vi.mock('./features/canvas/TaskNode.jsx', () => ({ default: () => null }));
vi.mock('./features/canvas/SourceNode.jsx', () => ({ default: () => null }));
vi.mock('./features/canvas/ToolNode.jsx', () => ({ default: () => null }));
vi.mock('./features/library/Palette.jsx', () => ({ default: () => <div>Node palette</div> }));
vi.mock('./features/library/ToolsPanel.jsx', () => ({ default: () => null }));
vi.mock('./features/memory/MemoryPanel.jsx', () => ({ default: () => null }));
vi.mock('./components/ReviewPanel.jsx', () => ({
  default: ({ open }) => open ? <div>Review overlay</div> : null,
}));
vi.mock('./features/execution/RunsPanel.jsx', () => ({
  default: ({ open }) => open ? <div>Runs overlay</div> : null,
}));
vi.mock('./features/execution/SchedulePanel.jsx', () => ({
  default: ({ open }) => open ? <div>Schedule overlay</div> : null,
}));
vi.mock('./features/memory/MemorySettings.jsx', () => ({ memorySiblings: () => [] }));
vi.mock('./features/chat/ChatPanel.jsx', () => ({
  default: () => <div data-testid="chat-panel">Chat panel</div>,
  renameChatHistory: vi.fn(),
}));
vi.mock('./features/workspace/WorkspacePanel.jsx', () => ({
  default: () => <div>Workspace is visible</div>,
}));
vi.mock('./features/canvas/Inspector.jsx', () => ({
  default: ({ onGraphChange, node, onRename, graph }) => (
    <>
      <button type="button" onClick={() => onGraphChange({ name: 'Renamed workflow' })}>
        Rename on canvas
      </button>
      <button type="button" onClick={() => onGraphChange({ goal: 'edited during save' })}>Edit goal</button>
      <span data-testid="goal">{graph?.goal}</span>
      {node && <button type="button" onClick={() => onRename(node.id, 'renamed_writer')}>Rename node</button>}
    </>
  ),
}));
vi.mock('./features/execution/RunDialog.jsx', () => ({
  default: ({ open, onSubmit }) => open
    ? <button type="button" onClick={() => onSubmit({})}>Start test run</button>
    : null,
}));
vi.mock('./features/evaluation/EvolvePanel.jsx', () => ({
  default: ({ open }) => open ? <div>Evolve overlay</div> : null,
}));
vi.mock('./components/TopBar.jsx', () => ({
  default: ({ dirty, onRun, onImport, onWorkspace, onToggleWatch, onReview, onRuns, onSave }) => (
    <div>
      <span data-testid="dirty">{dirty ? 'dirty' : 'clean'}</span>
      <button type="button" onClick={onSave}>Save</button>
      <button type="button" onClick={onRun}>Run</button>
      <button type="button" onClick={onImport}>Import</button>
      <button type="button" onClick={onWorkspace}>Workspace</button>
      <button type="button" onClick={onToggleWatch}>Watch</button>
      <button type="button" onClick={onReview}>Review</button>
      <button type="button" onClick={onRuns}>Runs</button>
    </div>
  ),
}));

const GRAPH = {
  id: 'old-id',
  name: 'Original workflow',
  goal: '',
  output_dir: 'runs',
  tasks: [],
  edges: [],
  workflow_inputs: [],
};

beforeEach(() => {
  vi.clearAllMocks();
  api.chatMemoryResources.mockResolvedValue({ resources: [] });
  api.canvasAgents.mockResolvedValue({ agents: [] });
  api.agentSessions.mockResolvedValue({ sessions: [] });
  state.layout = 'desktop';
  api.mem0Spaces.mockResolvedValue({ spaces: [] });
  api.palette.mockResolvedValue({ templates: [], sources: [] });
  api.listTemplates.mockResolvedValue({ templates: [] });
  api.listGraphs.mockResolvedValue([GRAPH]);
  api.getGraph.mockResolvedValue(GRAPH);
  api.getWatch.mockResolvedValue({ watching: false, watchers: [] });
  api.listBatches.mockResolvedValue({ batches: [] });
  api.saveGraph.mockResolvedValue({
    ...GRAPH,
    id: 'renamed-workflow',
    name: 'Renamed workflow',
  });
  api.runGraph.mockResolvedValue({ run_id: 'run-1' });
  api.startWatch.mockResolvedValue({ watching: true, watchers: [] });
});

async function loadedApp() {
  const user = userEvent.setup();
  const view = render(<App />);
  await waitFor(() => expect(api.getGraph).toHaveBeenCalledWith('old-id'));
  return { user, ...view };
}

describe('workflow-level interactions', () => {
  it('gives an empty canvas a direct path to Chat', async () => {
    const { user } = await loadedApp();
    expect(screen.getByRole('heading', { name: 'What should this workflow do?' }))
      .toBeInTheDocument();
    expect(screen.queryByTestId('chat-panel')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Ask Chat' }));
    expect(screen.getByTestId('chat-panel')).toBeInTheDocument();
  });

  it('keeps only one feature overlay active at a time', async () => {
    const { user } = await loadedApp();
    await user.click(screen.getByRole('button', { name: 'Review' }));
    expect(screen.getByText('Review overlay')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Runs' }));
    expect(screen.queryByText('Review overlay')).not.toBeInTheDocument();
    expect(screen.getByText('Runs overlay')).toBeInTheDocument();
  });

  it('creates only one initial workflow when StrictMode replays effects', async () => {
    api.listGraphs.mockResolvedValue([]);
    api.createGraph.mockResolvedValue(GRAPH);

    render(
      <React.StrictMode>
        <App />
      </React.StrictMode>
    );

    await waitFor(() => expect(api.createGraph).toHaveBeenCalledTimes(1));
  });

  it('runs with the id returned by saving a renamed workflow', async () => {
    const { user } = await loadedApp();
    await user.click(screen.getByRole('button', { name: 'Rename on canvas' }));
    await waitFor(() => expect(screen.getByTestId('dirty')).toHaveTextContent('dirty'));
    await user.click(screen.getByRole('button', { name: 'Run' }));
    await user.click(await screen.findByRole('button', { name: 'Start test run' }));

    await waitFor(() => expect(api.runGraph).toHaveBeenCalledWith(
      'renamed-workflow', {}, undefined, undefined, undefined, undefined));
  });

  it('asks before an import replaces unsaved canvas edits', async () => {
    const { container, user } = await loadedApp();
    await user.click(screen.getByRole('button', { name: 'Rename on canvas' }));
    await waitFor(() => expect(screen.getByTestId('dirty')).toHaveTextContent('dirty'));

    fireEvent.change(container.querySelector('input[type="file"]'), {
      target: { files: [new File(['{}'], 'graph.json', { type: 'application/json' })] },
    });

    expect(await screen.findByText('Discard unsaved changes?')).toBeInTheDocument();
    expect(api.importGraph).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(api.importGraph).not.toHaveBeenCalled();
  });

  it('starts watching with the id returned by save', async () => {
    const { user } = await loadedApp();
    await user.click(screen.getByRole('button', { name: 'Rename on canvas' }));
    await waitFor(() => expect(screen.getByTestId('dirty')).toHaveTextContent('dirty'));
    await user.click(screen.getByRole('button', { name: 'Watch' }));

    await waitFor(() => expect(api.startWatch).toHaveBeenCalledWith('renamed-workflow'));
  });

  it('opens Workspace visibly on a phone layout', async () => {
    state.layout = 'phone';
    const { user } = await loadedApp();
    expect(screen.queryByText('Workspace is visible')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Workspace' }));
    expect(await screen.findByText('Workspace is visible')).toBeInTheDocument();
  });

  it('opens Workspace in the tablet drawer', async () => {
    state.layout = 'tablet';
    const { user } = await loadedApp();
    expect(screen.queryByText('Workspace is visible')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Workspace' }));
    expect(await screen.findByText('Workspace is visible')).toBeInTheDocument();
  });
});


it('opens a requested project task without loading the global task list', async () => {
  api.projectTasks.mockResolvedValue([GRAPH]);
  render(<App projectId="project-1" initialGraphId="old-id" />);
  await waitFor(() => expect(api.getGraph).toHaveBeenCalledWith('old-id'));
  expect(api.projectTasks).toHaveBeenCalledWith('project-1');
  expect(api.listGraphs).not.toHaveBeenCalled();
});

it('prepares a run without starting a model call', async () => {
  api.projectTasks.mockResolvedValue([GRAPH]);
  render(<App projectId="project-1" initialGraphId="old-id" initialRun />);
  expect(await screen.findByText('Start test run')).toBeInTheDocument();
  expect(api.runGraph).not.toHaveBeenCalled();
});


it('connecting a memory source creates a compatible read-only reader', async () => {
  api.getGraph.mockResolvedValue({ ...GRAPH, tasks: [
    { name: 'writer', use_long_term_memory: true, memory: { match: 'company', at: 'as_of' }, inputs: [], outputs: [] },
    { name: 'reader', inputs: [], outputs: [] },
  ] });
  render(<App initialGraphId="old-id" />);
  await waitFor(() => expect(state.canvas.nodes.some(n => n.id === 'reader')).toBe(true));
  act(() => state.canvas.onConnect({ source: 'mem:writer', target: 'reader' }));
  await waitFor(() => expect(state.canvas.nodes.find(n => n.id === 'reader').data.memory).toMatchObject({
    read_enabled: true, write_enabled: false, read_from: ['writer'],
    kind: 'table', match: 'company', at: 'as_of',
    // Entity and time bindings resolve on their own (memory bindings v2):
    // a reader no longer has to "also record" them to be matched by them.
    context: [], time_filter: true,
  }));
  expect(state.canvas.edges.some(e => e.id === 'mw:reader')).toBe(false);
});

it('selects, disconnects, moves and saves one shared memory resource', async () => {
  const space = 'a'.repeat(32);
  const graph = { ...GRAPH, memory_resources: [{ space_id: space, name: 'Research' }], tasks: [
    { name: 'writer', use_long_term_memory: true, memory: { provider: 'mem0', space_id: space, read_enabled: false }, inputs: [], outputs: [] },
    { name: 'reader', use_long_term_memory: true, memory: { provider: 'mem0', space_id: space, write_enabled: false }, inputs: [], outputs: [] },
  ] };
  api.getGraph.mockResolvedValue(graph);
  render(<App initialGraphId="old-id" />);
  await waitFor(() => expect(state.canvas.nodes.filter(n => n.type === 'memory')).toHaveLength(1));
  const resourceId = `mem:space:${space}`;
  act(() => state.canvas.onNodeClick(null, { id: resourceId }));
  expect(await screen.findByRole('heading', { name: 'Research' })).toBeInTheDocument();
  act(() => state.canvas.onEdgesChange([{ type: 'select', id: 'mw:writer', selected: true }]));
  expect(state.canvas.edges.find(e => e.id === 'mw:writer').selected).toBe(true);
  act(() => state.canvas.onEdgesChange([{ type: 'remove', id: 'mw:writer' }]));
  expect(state.canvas.nodes.find(n => n.id === 'writer').data.memory.write_enabled).toBe(false);
  expect(state.canvas.edges.some(e => e.data?.memory === 'read')).toBe(true);
  act(() => state.canvas.onNodesChange([{ type: 'position', id: resourceId, position: { x: 222, y: 333 } }]));
  expect(state.canvas.nodes.find(n => n.id === resourceId).position).toEqual({ x: 222, y: 333 });
  const user = userEvent.setup();
  await user.click(screen.getByRole('button', { name: 'Run', exact: true }));
  await user.click(await screen.findByRole('button', { name: 'Start test run' }));
  await waitFor(() => expect(api.saveGraph).toHaveBeenCalled());
  const body = api.saveGraph.mock.calls.at(-1)[1];
  expect(body.memory_positions[resourceId]).toEqual({ x: 222, y: 333 });
  expect(body.memory_resources).toHaveLength(1);
  expect(body.tasks).toHaveLength(2);
  expect(body.edges).toEqual([]);
});

it('undoes and redoes a memory resource move', async () => {
  const space = 'b'.repeat(32);
  api.getGraph.mockResolvedValue({ ...GRAPH, memory_resources: [{ space_id: space, name: 'Research', x: 10, y: 20 }] });
  render(<App initialGraphId="old-id" />);
  const id = `mem:space:${space}`;
  await waitFor(() => expect(state.canvas.nodes.some(n => n.id === id)).toBe(true));
  act(() => state.canvas.onNodesChange([{ type: 'position', id, position: { x: 80, y: 90 } }]));
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 400)); });
  fireEvent.keyDown(window, { key: 'z', ctrlKey: true });
  await waitFor(() => expect(state.canvas.nodes.find(n => n.id === id).position).toEqual({ x: 10, y: 20 }));
  fireEvent.keyDown(window, { key: 'z', ctrlKey: true, shiftKey: true });
  await waitFor(() => expect(state.canvas.nodes.find(n => n.id === id).position).toEqual({ x: 80, y: 90 }));
});

it('returns from a memory inspector to workflow settings', async () => {
  const space = 'c'.repeat(32);
  api.getGraph.mockResolvedValue({ ...GRAPH, memory_resources: [{ space_id: space, name: 'Research' }] });
  render(<App initialGraphId="old-id" />);
  const id = `mem:space:${space}`;
  await waitFor(() => expect(state.canvas.nodes.some(n => n.id === id)).toBe(true));
  act(() => state.canvas.onNodeClick(null, { id }));
  await userEvent.setup().click(await screen.findByRole('button', { name: '← Back to workflow' }));
  expect(screen.queryByRole('heading', { name: 'Research' })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Rename on canvas' })).toBeInTheDocument();
  expect(state.canvas.nodes.find(n => n.id === id).selected).toBe(false);
});


it('creates a memory node with one click from Chat, then returns to workflow', async () => {
  const { user } = await loadedApp();
  const space = { id: 'd'.repeat(32), name: 'Shared memory' };
  api.createMem0Space.mockResolvedValue(space);
  await user.click(screen.getByRole('button', { name: 'Chat', exact: true }));
  await user.click(screen.getByRole('button', { name: '+ Add shared memory' }));
  expect(await screen.findByRole('heading', { name: 'Shared memory' })).toBeVisible();
  expect(state.canvas.nodes.filter(n => n.type === 'memory')).toHaveLength(1);
  expect(api.createMem0Space).toHaveBeenCalledTimes(1);
  expect(state.center).toHaveBeenCalled();
  await user.click(screen.getByRole('button', { name: '← Back to workflow' }));
  expect(screen.getByRole('button', { name: 'Rename on canvas' })).toBeVisible();
  expect(state.canvas.onSelectionChange).toBeUndefined();
  expect(api.runGraph).not.toHaveBeenCalled();
});

it('reports a failed memory creation without adding a broken node and allows retry', async () => {
  const { user } = await loadedApp();
  api.createMem0Space.mockRejectedValueOnce(new Error('Creation failed'));
  await user.click(screen.getByRole('button', { name: '+ Add shared memory' }));
  expect(await screen.findByText(/Creation failed/)).toBeVisible();
  expect(state.canvas.nodes.filter(n => n.type === 'memory')).toHaveLength(0);
  api.createMem0Space.mockResolvedValue({ id: 'e'.repeat(32), name: 'Shared memory' });
  await user.click(screen.getByRole('button', { name: '+ Add shared memory' }));
  expect(await screen.findByRole('heading', { name: 'Shared memory' })).toBeVisible();
});


it('returns from agent settings to the originating memory and then workflow', async () => {
  api.getGraph.mockResolvedValue({ ...GRAPH, tasks: [
    { name: 'writer', use_long_term_memory: true, memory: { match: 'company', at: 'as_of' }, inputs: [], outputs: [] },
  ] });
  render(<App initialGraphId="old-id" />);
  await waitFor(() => expect(state.canvas.nodes.some(n => n.id === 'mem:writer')).toBe(true));
  act(() => state.canvas.onNodeClick(null, { id: 'mem:writer' }));
  const user = userEvent.setup();
  const selectionBeforeNavigation = state.canvas.onSelectionChange;
  await user.click(await screen.findByRole('button', { name: 'writer →' }));
  // React Flow can deliver selection synchronization through the previous render callback.
  act(() => selectionBeforeNavigation?.({ nodes: [{ id: 'writer' }] }));
  act(() => state.canvas.onSelectionChange?.({ nodes: [] }));
  await user.click(screen.getByRole('button', { name: '← Back to memory' }));
  act(() => state.canvas.onSelectionChange?.({ nodes: [] }));
  expect(screen.getByRole('button', { name: 'writer →' })).toBeVisible();
  await user.click(screen.getByRole('button', { name: '← Back to workflow' }));
  expect(screen.queryByRole('button', { name: 'writer →' })).not.toBeInTheDocument();
  act(() => state.canvas.onNodeClick(null, { id: 'writer' }));
  await user.click(screen.getByRole('button', { name: '← Back to workflow' }));
  expect(screen.queryByRole('button', { name: /Back to/ })).not.toBeInTheDocument();
});

it('creates an independent Chat Agent, sends a message, and returns through harness settings', async () => {
  const { user } = await loadedApp();
  const agent = { id: 'agent1', name: 'Chat Agent 1', engine: 'deepagents', instructions: 'Help', provider: null, max_steps: 20, timeout: 300, toolkits: null, memories: [], x: 400, y: 250 };
  api.createCanvasAgent.mockResolvedValue(agent);
  api.createAgentSession.mockResolvedValue({ id: 'session1', created_at: 1, messages: [], events: [], status: 'idle' });
  api.sendAgentMessage.mockResolvedValue({ id: 'session1', created_at: 1, messages: [{ role: 'user', content: 'Investigate this' }], events: [], status: 'running' });
  await user.click(screen.getByRole('button', { name: '+ Add Chat Agent' }));
  expect(await screen.findByRole('heading', { name: 'Chat Agent 1' })).toBeVisible();
  expect(state.canvas.nodes.some(n => n.id === 'chat:agent1')).toBe(true);
  act(() => state.canvas.onNodesChange([{ id: 'chat:agent1', type: 'dimensions', dimensions: { width: 200, height: 80 } }]));
  expect(state.canvas.nodes.find(n => n.id === 'chat:agent1').measured).toEqual({ width: 200, height: 80 });
  const stableNodes = state.canvas.nodes;
  act(() => state.canvas.onNodesChange([{ id: 'chat:agent1', type: 'dimensions', dimensions: { width: 200, height: 80 } }]));
  expect(state.canvas.nodes).toBe(stableNodes);
  await user.click(screen.getByRole('button', { name: 'Harness settings →' }));
  expect(screen.getByLabelText('Maximum model steps')).toHaveValue(20);
  await user.click(screen.getByRole('button', { name: '← Back to chat' }));
  await user.type(screen.getByLabelText('Message Chat Agent'), 'Investigate this');
  await user.click(screen.getByRole('button', { name: 'Send', exact: true }));
  await waitFor(() => expect(api.sendAgentMessage).toHaveBeenCalledWith('old-id', 'agent1', 'session1', 'Investigate this'));
  await user.click(screen.getByRole('button', { name: '← Back to workflow' }));
  expect(screen.getByRole('button', { name: 'Rename on canvas' })).toBeVisible();
  expect(api.runGraph).not.toHaveBeenCalled();
  expect(api.saveGraph).not.toHaveBeenCalled();
});

it('connects and disconnects Chat Agent memory without creating workflow dependencies', async () => {
  const space = 'f'.repeat(32);
  const agent = { id: 'agent1', name: 'Chat Agent 1', engine: 'deepagents', instructions: '', max_steps: 20, timeout: 300, memories: [], x: 400, y: 250 };
  api.canvasAgents.mockResolvedValue({ agents: [agent] });
  api.getGraph.mockResolvedValue({ ...GRAPH, memory_resources: [{ space_id: space, name: 'Knowledge' }] });
  api.updateCanvasAgent.mockImplementation(async (_g, id, body) => ({ id, ...body }));
  render(<App initialGraphId="old-id" />);
  await waitFor(() => expect(state.canvas.nodes.some(n => n.id === 'chat:agent1')).toBe(true));
  act(() => state.canvas.onConnect({ source: `mem:space:${space}`, target: 'chat:agent1' }));
  await waitFor(() => expect(state.canvas.edges.some(e => e.id.startsWith('chat-read:'))).toBe(true));
  const edge = state.canvas.edges.find(e => e.id.startsWith('chat-read:'));
  act(() => state.canvas.onEdgesChange([{ type: 'remove', id: edge.id }]));
  await waitFor(() => expect(state.canvas.edges.some(e => e.id.startsWith('chat-read:'))).toBe(false));
  expect(api.saveGraph).not.toHaveBeenCalled();
});


it('reveals and locates existing legacy memories without modifying the workflow', async () => {
  localStorage.setItem('evoagentx-studio:show-memory', '0');
  api.getGraph.mockResolvedValue({ ...GRAPH, tasks: ['investigate', 'decide'].map(name => ({ name, use_long_term_memory: true, memory: { match: 'company', at: 'as_of' }, inputs: [], outputs: [] })) });
  render(<App initialGraphId="old-id" />);
  const button = await screen.findByRole('button', { name: 'Show and locate memories' });
  expect(state.canvas.nodes.filter(n => n.type === 'memory')).toHaveLength(0);
  await userEvent.setup().click(button);
  await waitFor(() => expect(state.fit).toHaveBeenCalledWith(expect.objectContaining({ nodes: [{ id: 'mem:investigate' }, { id: 'mem:decide' }] })));
  const memories = state.canvas.nodes.filter(n => n.type === 'memory');
  expect(memories).toHaveLength(2);
  expect(memories.every(n => n.initialWidth > 0 && n.initialHeight > 0)).toBe(true);
  expect(api.saveGraph).not.toHaveBeenCalled();
  expect(api.createMem0Space).not.toHaveBeenCalled();
  expect(localStorage.getItem('evoagentx-studio:show-memory')).toBe('1');
  localStorage.removeItem('evoagentx-studio:show-memory');
});


it('retains Memory measurements and read/write edges across inspector navigation', async () => {
  api.getGraph.mockResolvedValue({ ...GRAPH, tasks: ['investigate', 'decide'].map(name => ({ name, use_long_term_memory: true, memory: { match: 'company', at: 'as_of', read_from: name === 'investigate' ? ['investigate', 'decide'] : ['decide'] }, inputs: [], outputs: [] })) });
  render(<App initialGraphId="old-id" />);
  await waitFor(() => expect(state.canvas.nodes.some(n => n.id === 'mem:investigate')).toBe(true));
  act(() => state.canvas.onNodesChange(['investigate', 'decide'].map(name => ({ id: `mem:${name}`, type: 'dimensions', dimensions: { width: 220, height: 110 } }))));
  act(() => state.canvas.onNodeClick(null, { id: 'mem:investigate' }));
  await userEvent.setup().click(screen.getByRole('button', { name: 'investigate →' }));
  await userEvent.setup().click(screen.getByRole('button', { name: '← Back to memory' }));
  expect(state.canvas.nodes.find(n => n.id === 'mem:investigate').measured).toEqual({ width: 220, height: 110 });
  expect(state.canvas.edges.filter(e => e.data?.memory)).toHaveLength(5);
  expect(api.saveGraph).not.toHaveBeenCalled();
});


it('removes a legacy Memory node and all its connections without deleting its Agent', async () => {
  api.getGraph.mockResolvedValue({ ...GRAPH, tasks: ['investigate', 'decide'].map(name => ({ name, use_long_term_memory: true, memory: { match: 'company', read_from: ['investigate', 'decide'] }, inputs: [], outputs: [] })) });
  render(<App initialGraphId="old-id" />);
  await waitFor(() => expect(state.canvas.nodes.some(n => n.id === 'mem:investigate')).toBe(true));
  await act(async () => state.canvas.nodes.find(n => n.id === 'mem:investigate').data.onDelete('mem:investigate'));
  expect(state.canvas.nodes.some(n => n.id === 'mem:investigate')).toBe(false);
  expect(state.canvas.nodes.some(n => n.id === 'investigate')).toBe(true);
  expect(state.canvas.nodes.some(n => n.id === 'mem:decide')).toBe(true);
  expect(state.canvas.edges.some(e => e.source === 'mem:investigate' || e.target === 'mem:investigate')).toBe(false);
});

it('removes a Chat node through the archive API', async () => {
  api.canvasAgents.mockResolvedValue({ agents: [{ id: 'chat1', name: 'Chat', memories: [], x: 0, y: 0 }] });
  api.removeCanvasAgent.mockResolvedValue({ removed: true });
  render(<App initialGraphId="old-id" />);
  await waitFor(() => expect(state.canvas.nodes.some(n => n.id === 'chat:chat1')).toBe(true));
  await act(async () => state.canvas.nodes.find(n => n.id === 'chat:chat1').data.onDelete('chat:chat1'));
  expect(api.removeCanvasAgent).toHaveBeenCalledWith('old-id', 'chat1');
  expect(state.canvas.nodes.some(n => n.id === 'chat:chat1')).toBe(false);
});


it('connects named legacy Memory to Chat, updates inspector immediately, and removes access on disconnect', async () => {
  const agent = { id: 'agent1', name: 'Chat', instructions: '', max_steps: 20, timeout: 300, memories: [], x: 0, y: 0 };
  api.canvasAgents.mockResolvedValue({ agents: [agent] });
  api.chatMemoryResources.mockResolvedValue({ resources: [{ id: 'mem:investigate', name: 'investigate memory', kind: 'table', writable: false, write_reason: 'Owned by workflow' }] });
  api.getGraph.mockResolvedValue({ ...GRAPH, tasks: [{ name: 'investigate', use_long_term_memory: true, memory: { match: 'company' }, inputs: [], outputs: [] }] });
  api.updateCanvasAgent.mockImplementation(async (_g, id, body) => ({ id, ...body }));
  render(<App initialGraphId="old-id" />);
  await waitFor(() => expect(state.canvas.nodes.some(n => n.id === 'chat:agent1')).toBe(true));
  act(() => state.canvas.onConnect({ source: 'mem:investigate', target: 'chat:agent1' }));
  await waitFor(() => expect(state.canvas.edges.some(e => e.id.startsWith('chat-read:'))).toBe(true));
  expect(state.canvas.nodes.find(n => n.id === 'mem:investigate').data.readers).toContain('chat:agent1');
  act(() => state.canvas.onNodeClick(null, { id: 'chat:agent1' }));
  const user = userEvent.setup();
  await user.click(await screen.findByRole('button', { name: 'Harness settings →' }));
  expect(await screen.findByLabelText('investigate memory read')).toBeChecked();
  expect(screen.getByLabelText('investigate memory write')).toBeDisabled();
  await user.click(screen.getByLabelText('investigate memory read'));
  await waitFor(() => expect(state.canvas.edges.some(e => e.id.startsWith('chat-read:'))).toBe(false));
  expect(screen.getByLabelText('investigate memory read')).not.toBeChecked();
});

it('does not lose read access when read and write connections are saved together', async () => {
  const space = 'e'.repeat(32);
  api.canvasAgents.mockResolvedValue({ agents: [{ id: 'agent1', name: 'Chat', memories: [], x: 0, y: 0 }] });
  api.getGraph.mockResolvedValue({ ...GRAPH, memory_resources: [{ space_id: space, name: 'Findings' }] });
  api.updateCanvasAgent.mockImplementation(async (_g, id, body) => ({ id, ...body }));
  render(<App initialGraphId="old-id" />);
  await waitFor(() => expect(state.canvas.nodes.some(n => n.id === 'chat:agent1')).toBe(true));
  act(() => {
    state.canvas.onConnect({ source: `mem:space:${space}`, target: 'chat:agent1' });
    state.canvas.onConnect({ source: 'chat:agent1', target: `mem:space:${space}` });
  });
  await waitFor(() => expect(state.canvas.edges.filter(e => e.data?.chatAgent)).toHaveLength(2));
  expect(api.updateCanvasAgent.mock.calls.at(-1)[2].memories).toEqual([expect.objectContaining({ space_id: space, read: true, write: true })]);
});

for (const side of ['left', 'right', 'bottom', 'top']) {
  it(`retains Chat Memory ${side} handles and supports selecting and deleting the edge`, async () => {
    api.canvasAgents.mockResolvedValue({ agents: [{ id: 'agent1', name: 'Chat', memories: [], x: 0, y: 0 }] });
    api.getGraph.mockResolvedValue({ ...GRAPH, tasks: [{ name: 'investigate', use_long_term_memory: true, memory: { match: 'company' }, inputs: [], outputs: [] }] });
    api.updateCanvasAgent.mockImplementation(async (_g, id, body) => ({ id, ...body }));
    render(<App initialGraphId="old-id" />);
    await waitFor(() => expect(state.canvas.nodes.some(n => n.id === 'chat:agent1')).toBe(true));
    const handle = side === 'top' ? 's-out' : `s-out-${side}`;
    act(() => state.canvas.onConnect({ source: 'mem:investigate', target: 'chat:agent1', sourceHandle: handle, targetHandle: 't-in' }));
    await waitFor(() => expect(state.canvas.edges.find(e => e.data?.chatAgent)?.sourceHandle).toBe(handle));
    const id = state.canvas.edges.find(e => e.data?.chatAgent).id;
    expect(state.canvas.edges.find(e => e.id === id).targetHandle).toBe('t-in');
    act(() => state.canvas.onEdgesChange([{ id, type: 'select', selected: true }]));
    expect(state.canvas.edges.find(e => e.id === id).selected).toBe(true);
    act(() => state.canvas.onEdgesChange([{ id, type: 'remove' }]));
    await waitFor(() => expect(state.canvas.edges.some(e => e.id === id)).toBe(false));
    expect(api.updateCanvasAgent.mock.calls.at(-1)[2].memories).toEqual([]);
  });
}

it('continues a failed Chat session without creating a new conversation', async () => {
  const agent = { id: 'agent1', name: 'Chat', memories: [], x: 0, y: 0 };
  const session = { id: 'existing', created_at: 1, status: 'failed', messages: [{ role: 'user', content: 'Earlier question' }], events: [], error: 'Model connection failed' };
  api.canvasAgents.mockResolvedValue({ agents: [agent] });
  api.agentSessions.mockResolvedValue({ sessions: [session] });
  api.sendAgentMessage.mockResolvedValue({ ...session, status: 'running', error: null });
  render(<App initialGraphId="old-id" />);
  await waitFor(() => expect(state.canvas.nodes.some(n => n.id === 'chat:agent1')).toBe(true));
  act(() => state.canvas.onNodeClick(null, { id: 'chat:agent1' }));
  await screen.findByText('Earlier question');
  const user = userEvent.setup();
  await user.type(screen.getByLabelText('Message Chat Agent'), 'Continue');
  await user.click(screen.getByRole('button', { name: 'Send', exact: true }));
  await waitFor(() => expect(api.sendAgentMessage).toHaveBeenCalledWith('old-id', 'agent1', 'existing', 'Continue'));
  expect(api.createAgentSession).not.toHaveBeenCalled();
});

it('deletes the selected Chat conversation and ignores stale polling results', async () => {
 const session = { id: 'old-session', created_at: 1, status: 'idle', messages: [{ role: 'user', content: 'Old question' }], events: [] };
 api.canvasAgents.mockResolvedValue({ agents: [{ id: 'agent1', name: 'Chat', memories: [], x: 0, y: 0 }] });
 api.agentSessions.mockResolvedValue({ sessions: [session] });
 api.deleteAgentSession.mockResolvedValue({ removed: true });
 render(<App initialGraphId="old-id" />);
 await waitFor(() => expect(state.canvas.nodes.some(n => n.id === 'chat:agent1')).toBe(true));
 act(() => state.canvas.onNodeClick(null, { id: 'chat:agent1' }));
 await screen.findByText('Old question');
 const user = userEvent.setup();
 await user.click(screen.getByText('Delete conversation'));
 expect(api.deleteAgentSession).not.toHaveBeenCalled();
 await user.click(screen.getByText('Confirm delete'));
 await waitFor(() => expect(screen.queryByText('Old question')).not.toBeInTheDocument());
 expect(api.deleteAgentSession).toHaveBeenCalledWith('old-id', 'agent1', 'old-session');
});
it('initializes Memory and late Chat ports without waiting for measurement events', async () => {
  let resolveAgents;
  api.canvasAgents.mockImplementation(() => new Promise(resolve => { resolveAgents = resolve; }));
  api.getGraph.mockResolvedValue({ ...GRAPH, tasks: [{ name: 'investigate', use_long_term_memory: true, memory: { match: 'company' }, inputs: [], outputs: [] }] });
  render(<App initialGraphId="old-id" />);
  await waitFor(() => expect(state.canvas.nodes.find(n => n.id === 'mem:investigate')?.handles).toHaveLength(8));
  await act(async () => resolveAgents({ agents: [{ id: 'late', name: 'Late chat', x: 400, y: 200, memories: [{ memory_id: 'mem:investigate', read: true, read_source_handle: 's-out-bottom', read_target_handle: 't-in' }] }] }));
  const memory = state.canvas.nodes.find(n => n.id === 'mem:investigate');
  const chat = state.canvas.nodes.find(n => n.id === 'chat:late');
  expect(memory.measured).toBeUndefined();
  expect(chat.handles.find(h => h.id === 't-in')).toBeDefined();
  expect(memory.handles.find(h => h.id === 's-out-bottom')).toBeDefined();
  expect(state.canvas.edges.find(e => e.id.startsWith('chat-read:'))).toMatchObject({ sourceHandle: 's-out-bottom', targetHandle: 't-in' });
  expect(api.saveGraph).not.toHaveBeenCalled();
});

it('asks for a field mapping instead of adding an unmatched order-only edge', async () => {
  api.getGraph.mockResolvedValue({ ...GRAPH, tasks: [
    { name: 'extract', inputs: [], outputs: [{ name: 'result', type: 'str' }] },
    { name: 'decide', inputs: [{ name: 'evidence', type: 'str' }], outputs: [] },
  ], edges: [] });
  render(<App initialGraphId="old-id" />);
  await waitFor(() => expect(state.canvas.nodes.some(n => n.id === 'decide')).toBe(true));
  act(() => state.canvas.onConnect({ source: 'extract', target: 'decide' }));
  expect(screen.getByRole('dialog', { name: 'Edit connection' })).toBeInTheDocument();
  expect(state.canvas.edges.filter(e => e.source === 'extract')).toHaveLength(0);
  fireEvent.change(screen.getByLabelText('Source output for evidence'), { target: { value: 'result' } });
  fireEvent.click(screen.getByText('Save connection'));
  await waitFor(() => expect(state.canvas.edges.find(e => e.source === 'extract').data).toMatchObject({
    control_only: false, mappings: [{ from: 'result', to: 'evidence' }],
  }));
  act(() => state.canvas.onEdgeClick({}, state.canvas.edges.find(e => e.source === 'extract')));
  expect(screen.getByRole('dialog', { name: 'Edit connection' })).toBeInTheDocument();
  fireEvent.click(screen.getByText('Delete connection'));
  await waitFor(() => expect(state.canvas.edges.filter(e => e.source === 'extract')).toHaveLength(0));
});


it('undoes a graph setting immediately and redoes it without waiting for debounce', async () => {
  const { user } = await loadedApp();
  await user.click(screen.getByText('Rename on canvas'));
  expect(screen.getByTestId('dirty')).toHaveTextContent('dirty');
  fireEvent.keyDown(window, {key:'z', ctrlKey:true});
  await waitFor(() => expect(screen.getByTestId('dirty')).toHaveTextContent('clean'));
  fireEvent.keyDown(window, {key:'z', ctrlKey:true, shiftKey:true});
  await waitFor(() => expect(screen.getByTestId('dirty')).toHaveTextContent('dirty'));
});

it('undo restores an archived Chat node through the server without losing sessions', async () => {
  const agent = {id:'a'.repeat(32),name:'Research',memories:[],x:0,y:0};
  api.canvasAgents.mockResolvedValue({agents:[agent]});
  api.removeCanvasAgent.mockResolvedValue({removed:true});
  render(<App initialGraphId="old-id" />);
  await waitFor(() => expect(state.canvas.nodes.some(n => n.id === `chat:${agent.id}`)).toBe(true));
  await act(async () => state.canvas.nodes.find(n => n.id === `chat:${agent.id}`).data.onDelete(`chat:${agent.id}`));
  await waitFor(() => expect(state.canvas.nodes.some(n => n.id === `chat:${agent.id}`)).toBe(false));
  fireEvent.keyDown(window, {key:'z',ctrlKey:true});
  await waitFor(() => expect(api.restoreCanvasAgents).toHaveBeenCalledWith('old-id',[agent]));
  await waitFor(() => expect(state.canvas.nodes.some(n => n.id === `chat:${agent.id}`)).toBe(true));
});


it('renaming a memory owner keeps readers, positions and chat agents pointed at its store', async () => {
  api.getGraph.mockResolvedValue({ ...GRAPH, memory_positions: { 'mem:writer': { x: 5, y: 6 } }, tasks: [
    { name: 'writer', use_long_term_memory: true, memory: {}, inputs: [], outputs: [] },
    { name: 'reader', use_long_term_memory: true, memory: { read_from: ['reader', 'writer'], write_enabled: false,
      canvas_connections: { 'mem:writer:read': { sourceHandle: 's-out', targetHandle: 'b-in' } } }, inputs: [], outputs: [] },
  ] });
  const agent = { id: 'ag1', name: 'Chat', x: 0, y: 0, memories: [{ memory_id: 'mem:writer', read: true, write: false }] };
  api.canvasAgents.mockResolvedValue({ agents: [agent] });
  api.updateCanvasAgent.mockImplementation(async (_g, id, settings) => ({ id, ...settings }));
  render(<App initialGraphId="old-id" />);
  await waitFor(() => expect(state.canvas.nodes.some(n => n.id === 'reader')).toBe(true));
  await waitFor(() => expect(state.canvas.nodes.some(n => n.id === 'chat:ag1')).toBe(true));
  act(() => state.canvas.onNodeClick(null, { id: 'writer' }));
  const user = userEvent.setup();
  await user.click(await screen.findByRole('button', { name: 'Rename node' }));
  await waitFor(() => expect(state.canvas.nodes.some(n => n.id === 'renamed_writer')).toBe(true));
  const reader = state.canvas.nodes.find(n => n.id === 'reader');
  expect(reader.data.memory.read_from).toEqual(['reader', 'renamed_writer']);
  expect(Object.keys(reader.data.memory.canvas_connections)).toEqual(['mem:writer:read']);
  expect(state.canvas.edges.some(e => e.data?.memory === 'read' && e.data.agent === 'reader' && e.data.from === 'renamed_writer')).toBe(true);
  expect(state.canvas.nodes.find(n => n.id === 'mem:writer').position).toEqual({ x: 5, y: 6 });
  expect(state.canvas.edges.some(e => e.source === 'mem:writer' && e.target === 'chat:ag1')).toBe(true);
  expect(api.updateCanvasAgent).not.toHaveBeenCalled();
});

it('keeps settings edited while a save is in flight', async () => {
  let finish;
  api.saveGraph.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
  const { user } = await loadedApp();
  await user.click(screen.getByRole('button', { name: 'Rename on canvas' }));
  await user.click(screen.getByRole('button', { name: 'Save' }));
  await waitFor(() => expect(api.saveGraph).toHaveBeenCalled());
  await user.click(screen.getByRole('button', { name: 'Edit goal' }));
  expect(screen.getByTestId('goal')).toHaveTextContent('edited during save');
  await act(async () => { finish({ ...GRAPH, name: 'Renamed workflow', goal: '' }); });
  expect(screen.getByTestId('goal')).toHaveTextContent('edited during save');
  await waitFor(() => expect(screen.getByTestId('dirty')).toHaveTextContent('dirty'));
});
