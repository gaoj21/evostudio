import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import App from './App.jsx';

const state = vi.hoisted(() => ({ layout: 'desktop' }));
const api = vi.hoisted(() => ({
  palette: vi.fn(),
  listTemplates: vi.fn(),
  listGraphs: vi.fn(),
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
    ReactFlow: ({ children }) => <div data-testid="canvas">{children}</div>,
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
    useReactFlow: () => ({ screenToFlowPosition: (point) => point }),
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

vi.mock('./components/TaskNode.jsx', () => ({ default: () => null }));
vi.mock('./components/SourceNode.jsx', () => ({ default: () => null }));
vi.mock('./components/ToolNode.jsx', () => ({ default: () => null }));
vi.mock('./components/Palette.jsx', () => ({ default: () => <div>Node palette</div> }));
vi.mock('./components/ToolsPanel.jsx', () => ({ default: () => null }));
vi.mock('./components/MemoryPanel.jsx', () => ({ default: () => null }));
vi.mock('./components/ReviewPanel.jsx', () => ({ default: () => null }));
vi.mock('./components/RunsPanel.jsx', () => ({ default: () => null }));
vi.mock('./components/SchedulePanel.jsx', () => ({ default: () => null }));
vi.mock('./components/MemorySettings.jsx', () => ({ memorySiblings: () => [] }));
vi.mock('./components/ChatPanel.jsx', () => ({
  default: () => <div>Chat</div>,
  renameChatHistory: vi.fn(),
}));
vi.mock('./components/WorkspacePanel.jsx', () => ({
  default: () => <div>Workspace is visible</div>,
}));
vi.mock('./components/Inspector.jsx', () => ({
  default: ({ onGraphChange }) => (
    <button type="button" onClick={() => onGraphChange({ name: 'Renamed workflow' })}>
      Rename on canvas
    </button>
  ),
}));
vi.mock('./components/RunDialog.jsx', () => ({
  default: ({ open, onSubmit }) => open
    ? <button type="button" onClick={() => onSubmit({})}>Start test run</button>
    : null,
}));
vi.mock('./components/EvolvePanel.jsx', () => ({ default: () => null }));
vi.mock('./components/TopBar.jsx', () => ({
  default: ({ dirty, onRun, onImport, onWorkspace, onToggleWatch }) => (
    <div>
      <span data-testid="dirty">{dirty ? 'dirty' : 'clean'}</span>
      <button type="button" onClick={onRun}>Run</button>
      <button type="button" onClick={onImport}>Import</button>
      <button type="button" onClick={onWorkspace}>Workspace</button>
      <button type="button" onClick={onToggleWatch}>Watch</button>
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
  state.layout = 'desktop';
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
      'renamed-workflow', {}, undefined, undefined));
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
});
