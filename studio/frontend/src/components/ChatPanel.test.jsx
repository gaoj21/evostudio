import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import ChatPanel, { summarise } from './ChatPanel.jsx';

vi.mock('../api.js', () => ({
  api: {
    chatGraph: vi.fn(),
    chatJob: vi.fn(),
    runningChatJob: vi.fn(),
  },
}));

const { api } = await import('../api.js');

const GRAPH = { name: 'demo', tasks: [{ name: 'summarise' }] };

function setup(props = {}) {
  const onApply = vi.fn();
  const onRunRequest = vi.fn().mockResolvedValue('run-77');
  const view = render(
    <ChatPanel
      graphId="g1"
      getGraph={() => GRAPH}
      onApply={onApply}
      onRunRequest={onRunRequest}
      {...props}
    />
  );
  return { ...view, onApply, onRunRequest, user: userEvent.setup() };
}

async function send(user, text) {
  await user.type(screen.getByRole('textbox'), text);
  await user.click(screen.getByRole('button', { name: 'Send' }));
}

beforeEach(() => {
  window.localStorage.clear();
  api.runningChatJob.mockResolvedValue(null);
  api.chatGraph.mockResolvedValue({ reply: 'ok', operations: [] });
});

describe('summarise', () => {
  it('names each operation in plain words', () => {
    expect(summarise([{ op: 'add_node' }, { op: 'auto_layout' }]))
      .toBe('added · re-laid out');
  });

  it('counts repeats instead of repeating itself', () => {
    expect(summarise([{ op: 'add_node' }, { op: 'add_node' }, { op: 'add_edge' }]))
      .toBe('added ×2 · edge');
  });

  it('falls back to the raw name for an operation it has no wording for', () => {
    expect(summarise([{ op: 'teleport_node' }])).toBe('teleport_node');
  });

  it('says nothing when the turn changed nothing', () => {
    expect(summarise([])).toBe('');
    expect(summarise(undefined)).toBe('');
  });
});

describe('ChatPanel', () => {
  it('sends the canvas with the message and applies what comes back', async () => {
    const next = { name: 'demo', tasks: [{ name: 'summarise' }, { name: 'report' }] };
    api.chatGraph.mockResolvedValue({
      reply: 'Added a reporting step.',
      operations: [{ op: 'add_node' }],
      applied: true,
      graph: next,
    });
    const { onApply, user } = setup();

    await send(user, 'add a reporting step');

    await waitFor(() => expect(onApply).toHaveBeenCalledWith(next));
    expect(api.chatGraph).toHaveBeenCalledWith('g1', expect.objectContaining({
      message: 'add a reporting step',
      graph: GRAPH,
    }));
    expect(await screen.findByText('Added a reporting step.')).toBeInTheDocument();
    expect(screen.getByText('✓ added')).toBeInTheDocument();
  });

  it('undoes a turn by restoring the canvas as it was before it', async () => {
    api.chatGraph.mockResolvedValue({
      reply: 'Done.',
      operations: [{ op: 'delete_node' }],
      applied: true,
      graph: { name: 'demo', tasks: [] },
    });
    const { onApply, user } = setup();

    await send(user, 'delete the summarise step');
    await user.click(await screen.findByRole('button', { name: 'Undo this change' }));

    // The snapshot is the graph as it was *before* the turn, not the reply.
    expect(onApply).toHaveBeenLastCalledWith(GRAPH);
    expect(screen.getByText('Change undone.')).toBeInTheDocument();
    // Undoing twice would re-apply a stale canvas over later edits.
    expect(screen.queryByRole('button', { name: 'Undo this change' })).not.toBeInTheDocument();
  });

  it('offers no undo for a turn that only read the workflow', async () => {
    api.chatGraph.mockResolvedValue({
      reply: 'The second node writes to output/.',
      operations: [{ op: 'inspect_node' }],
      applied: false,
    });
    const { user } = setup();

    await send(user, 'what does the second node do?');

    expect(await screen.findByText('✓ read a node')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Undo this change' })).not.toBeInTheDocument();
  });

  it('waits for the user before starting a run the model proposed', async () => {
    api.chatGraph.mockResolvedValue({
      reply: 'Ready to run.',
      operations: [{ op: 'run_workflow' }],
      pending_run: { inputs: { city: 'Lima' } },
    });
    const { onRunRequest, user } = setup();

    await send(user, 'run it');

    expect(await screen.findByText(/Run this workflow\?/)).toHaveTextContent('city=Lima');
    expect(onRunRequest).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: 'Run it' }));
    await waitFor(() => expect(onRunRequest).toHaveBeenCalledWith({ city: 'Lima' }));
    expect(await screen.findByText('▶ run run-77 started')).toBeInTheDocument();
  });

  it('starts nothing when the user declines the proposed run', async () => {
    api.chatGraph.mockResolvedValue({
      reply: 'Ready to run.',
      operations: [{ op: 'run_workflow' }],
      pending_run: { inputs: {} },
    });
    const { onRunRequest, user } = setup();

    await send(user, 'run it');
    await user.click(await screen.findByRole('button', { name: 'Not now' }));

    expect(onRunRequest).not.toHaveBeenCalled();
    expect(screen.getByText('Run declined.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Run it' })).not.toBeInTheDocument();
  });

  it('reports the run outcome back to the agent once, without echoing it as a question', async () => {
    api.chatGraph.mockResolvedValue({
      reply: 'Ready.',
      operations: [],
      pending_run: { inputs: {} },
    });
    const { user, rerender } = setup();

    await send(user, 'run it');
    await user.click(await screen.findByRole('button', { name: 'Run it' }));
    await screen.findByText('▶ run run-77 started');
    api.chatGraph.mockResolvedValue({ reply: 'It failed on step 2.', operations: [] });

    const outcome = { run_id: 'run-77', status: 'failed' };
    const common = { graphId: 'g1', getGraph: () => GRAPH, onApply: vi.fn(), onRunRequest: vi.fn() };
    rerender(<ChatPanel {...common} runOutcome={outcome} />);

    await waitFor(() => expect(api.chatGraph).toHaveBeenCalledTimes(2));
    expect(api.chatGraph.mock.calls[1][1].message).toMatch(/run-77.*"failed"/);
    expect(await screen.findByText('It failed on step 2.')).toBeInTheDocument();

    // A re-render with the same outcome must not ask the agent all over again.
    rerender(<ChatPanel {...common} runOutcome={{ ...outcome }} />);
    await new Promise((r) => setTimeout(r, 20));
    expect(api.chatGraph).toHaveBeenCalledTimes(2);
  });

  it('shows the backend error instead of dropping the turn', async () => {
    api.chatGraph.mockRejectedValue({ body: { detail: 'no LLM configured' } });
    const { user } = setup();

    await send(user, 'build me something');

    expect(await screen.findByText('no LLM configured')).toBeInTheDocument();
    // Still usable afterwards — a failed turn must not leave the panel busy.
    expect(screen.getByRole('textbox')).toBeEnabled();
    await user.type(screen.getByRole('textbox'), 'try again');
    expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled();
  });

  it('picks up a generation that was still running', async () => {
    api.runningChatJob.mockResolvedValue({ job_id: 'j9', stage: 'Designing agents', elapsed: 12 });
    const generated = { name: 'demo', tasks: [{ name: 'fetch' }, { name: 'report' }] };
    api.chatJob.mockResolvedValue({ status: 'done', graph: generated, elapsed: 31 });
    const { onApply } = setup();

    expect(await screen.findByText(/Picking up the workflow generation/)).toBeInTheDocument();
    expect(screen.getByText('Designing agents (12s)')).toBeInTheDocument();

    // The first poll is one interval away, so give it room.
    expect(await screen.findByText('✓ generated 2 nodes in 31s', {}, { timeout: 4000 }))
      .toBeInTheDocument();
    // The result lands on the canvas even though nothing on this page asked for it.
    expect(onApply).toHaveBeenCalledWith(generated);
  });

  it('does not pick the same generation up twice as the canvas changes', async () => {
    // The server keeps listing a job for a moment after it settles, and every
    // canvas edit changes getGraph's identity. That combination used to add a
    // second "picking up" turn — and a second poll — for one generation.
    api.runningChatJob.mockResolvedValue({ job_id: 'j9', stage: 'Designing agents', elapsed: 12 });
    api.chatJob.mockResolvedValue({
      status: 'done', graph: { name: 'demo', tasks: [{ name: 'fetch' }] }, elapsed: 31,
    });
    const { onApply, rerender } = setup();
    await screen.findByText('✓ generated 1 nodes in 31s', {}, { timeout: 4000 });

    rerender(
      <ChatPanel graphId="g1" getGraph={() => ({ ...GRAPH })} onApply={onApply} onRunRequest={vi.fn()} />
    );
    await new Promise((r) => setTimeout(r, 50));

    expect(screen.getAllByText(/Picking up the workflow generation/)).toHaveLength(1);
    expect(api.chatJob).toHaveBeenCalledTimes(1);
  });

  it('keeps each workflow to its own conversation', async () => {
    api.chatGraph.mockResolvedValue({ reply: 'Noted for g1.', operations: [] });
    const { rerender, user } = setup();
    await send(user, 'remember this');
    await screen.findByText('Noted for g1.');

    rerender(<ChatPanel graphId="g2" getGraph={() => GRAPH} onApply={vi.fn()} onRunRequest={vi.fn()} />);
    expect(screen.queryByText('Noted for g1.')).not.toBeInTheDocument();

    rerender(<ChatPanel graphId="g1" getGraph={() => GRAPH} onApply={vi.fn()} onRunRequest={vi.fn()} />);
    expect(await screen.findByText('Noted for g1.')).toBeInTheDocument();
  });

  it('refuses to send while a turn is in flight', async () => {
    let release;
    api.chatGraph.mockReturnValue(new Promise((r) => { release = r; }));
    const { user } = setup();

    await send(user, 'first');
    expect(screen.getByRole('button', { name: 'Working…' })).toBeDisabled();
    expect(screen.getByRole('textbox')).toBeDisabled();

    release({ reply: 'done', operations: [] });
    await screen.findByText('done');
    expect(api.chatGraph).toHaveBeenCalledTimes(1);
  });
});
