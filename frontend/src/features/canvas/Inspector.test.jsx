import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../api.js', () => ({
  api: { listSourceTypes: vi.fn(), sourceInfo: vi.fn(), probeSource: vi.fn(), listDataResources: vi.fn(), listDatasets: vi.fn() },
}));
const { api } = await import('../../api.js');
import Inspector from './Inspector.jsx';

const TYPES = [
  { type: 'http_api', label: 'HTTP API', config: [{ name: 'url', label: 'URL', type: 'text', default: '' }], outputs: ['body'] },
  { type: 'project_feed', label: 'Project feed', local: true, has_info: true, project: 'demo',
    sequence: { group: 'ticket', order: 'day' },
    config: [{ name: 'version', label: 'Version', type: 'select', options: ['v1', 'v2'], default: 'v1' },
      { name: 'n', label: 'Count', type: 'number', default: 1 }],
    outputs: ['ticket', 'day', 'body'] },
];

const sourceNode = (source) => ({ id: 'input', type: 'source', position: { x: 0, y: 0 },
  data: { kind: 'source', description: '', inputs: [], outputs: [], ...(source ? { source } : {}) } });

beforeEach(() => {
  vi.clearAllMocks();
  api.listSourceTypes.mockResolvedValue({ source_types: TYPES });
  api.sourceInfo.mockResolvedValue({ versions: ['v1', 'v2'], count: 12 });
});

describe('the Input inspector is driven by the schema', () => {
  it('asks for a type instead of assuming one, and starts from its schema', async () => {
    const onUpdate = vi.fn();
    render(<Inspector node={sourceNode()} onUpdate={onUpdate} onRename={vi.fn()} />);
    const select = await screen.findByLabelText('Input type');
    expect([...select.querySelectorAll('option')].map((o) => o.value)).toEqual(['', 'http_api', 'project_feed']);
    expect(screen.queryByRole('button', { name: 'Use DataLoader preprocessing' })).not.toBeInTheDocument();
    await userEvent.setup().selectOptions(select, 'project_feed');
    expect(onUpdate).toHaveBeenCalledWith('input', {
      source: { type: 'project_feed', version: 'v1', n: 1 },
      outputs: [expect.objectContaining({ name: 'ticket' }), expect.objectContaining({ name: 'day' }), expect.objectContaining({ name: 'body' })],
    });
  });

  it('renders a plugin type with the generic form, its sequence and its details', async () => {
    const onUpdate = vi.fn();
    render(<Inspector node={sourceNode({ type: 'project_feed', version: 'v2', n: 3 })} onUpdate={onUpdate} onRename={vi.fn()} />);
    const version = await screen.findByLabelText('Version');
    expect(version).toHaveValue('v2');
    expect(screen.getByLabelText('Count')).toHaveValue(3);
    expect(screen.getByTestId('source-sequence')).toHaveTextContent('Records sharing ticket form one trajectory, run in day order.');
    await waitFor(() => expect(api.sourceInfo).toHaveBeenCalledWith('project_feed', { version: 'v2', n: 3 }));
    expect(await screen.findByText('Input details')).toBeInTheDocument();
    await userEvent.setup().selectOptions(version, 'v1');
    // Only the field changed: no hidden split or step rules ride along.
    expect(onUpdate).toHaveBeenLastCalledWith('input', { source: { type: 'project_feed', version: 'v1', n: 3 } });
  });

  it('wraps a type in a DataLoader with the group and order its schema declares', async () => {
    const onUpdate = vi.fn();
    const user = userEvent.setup();
    const { unmount } = render(<Inspector node={sourceNode({ type: 'project_feed', version: 'v1' })} onUpdate={onUpdate} onRename={vi.fn()} />);
    await screen.findByLabelText('Version');
    await user.click(screen.getByRole('button', { name: 'Use DataLoader preprocessing' }));
    expect(onUpdate.mock.calls.at(-1)[1].source).toMatchObject({ type: 'dataloader', group_by: 'ticket', order_by: 'day' });
    unmount();

    render(<Inspector node={sourceNode({ type: 'http_api', url: 'https://x' })} onUpdate={onUpdate} onRename={vi.fn()} />);
    await screen.findByLabelText('URL');
    await user.click(screen.getByRole('button', { name: 'Use DataLoader preprocessing' }));
    const wrapped = onUpdate.mock.calls.at(-1)[1].source;
    expect(wrapped).not.toHaveProperty('group_by');
    expect(wrapped).not.toHaveProperty('order_by');
    expect(api.sourceInfo).not.toHaveBeenCalledWith('http_api', expect.anything());
  });
});
