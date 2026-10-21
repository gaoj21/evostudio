import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import ChatPanel, { summarise, conversationHistory, appliedOperations } from './ChatPanel.jsx';

vi.mock('../../api.js', () => ({
  api: {
    chatGraph: vi.fn(),
    replayChatOperations: vi.fn(),
    stopAssistant: vi.fn(),
    stopChatJob: vi.fn(),
    chatJob: vi.fn(),
    runningChatJob: vi.fn(),
    getRun: vi.fn(),
  },
}));

const { api } = await import('../../api.js');

const GRAPH = { name: 'demo', tasks: [{ name: 'summarise' }] };

function setup(props = {}) {
  const onApply = vi.fn();
  const onRunRequest = vi.fn().mockResolvedValue({ ok: true, runId: 'run-77' });
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
    }), expect.objectContaining({signal:expect.anything()}));
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

    expect(await screen.findByText(/Run this workflow\?/)).toBeInTheDocument();
    expect(screen.getByDisplayValue('Lima')).toBeInTheDocument();
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
    expect(screen.getByRole('button', { name: '■ Stop' })).toBeEnabled();
    expect(screen.getByRole('textbox')).toBeDisabled();

    release({ reply: 'done', operations: [] });
    await screen.findByText('done');
    expect(api.chatGraph).toHaveBeenCalledTimes(1);
  });
});


describe('a run that did not start is not reported as started', () => {
  it('shows the refusal instead of "started" when no run id came back', async () => {
    api.chatGraph.mockResolvedValue({
      reply: 'Ready to run.',
      operations: [{ op: 'run_workflow' }],
      pending_run: { inputs: { city: 'Lima' } },
    });
    const { onRunRequest, user } = setup();
    onRunRequest.mockResolvedValue({ ok: false, error: 'Missing required input city' });

    await send(user, 'run it');
    await user.click(await screen.findByRole('button', { name: 'Run it' }));

    expect(await screen.findByText(/Missing required input city/)).toBeInTheDocument();
    expect(screen.queryByText(/started/)).not.toBeInTheDocument();
  });
});

it('does not show generation success before the generated graph validates', async () => {
  api.chatGraph.mockResolvedValue({ reply: 'Planning workflow', operations: [{ op: 'generate_workflow' }], job_id: 'bad', applied: false, errors: [] });
  api.chatJob.mockResolvedValue({ status: 'failed', error: 'Generated edge is invalid' });
  const { user, onApply } = setup();
  await send(user, 'Generate a workflow');
  expect(screen.queryByText('✓ generated workflow')).not.toBeInTheDocument();
  await screen.findByText('Generated edge is invalid', {}, { timeout: 4000 });
  expect(screen.queryByText('✓ generated workflow')).not.toBeInTheDocument();
  expect(onApply).not.toHaveBeenCalled();
});


it('carries completed generation and later edits into subsequent messages', async () => {
  let current = { name: 'demo', tasks: [], edges: [] };
  const generated = { name: 'demo', tasks: [{ name: 'read' }], edges: [] };
  const expanded = { name: 'demo', tasks: [{ name: 'read' }, { name: 'clean' }], edges: [] };
  api.chatGraph.mockResolvedValueOnce({ reply: 'Generating', operations: [{ op: 'generate_workflow' }], job_id: 'multi' })
    .mockResolvedValueOnce({ reply: 'Added cleaning', operations: [{ op: 'add_node' }], graph: expanded, applied: true })
    .mockResolvedValueOnce({ reply: 'Removed cleaning', operations: [{ op: 'delete_node' }], graph: generated, applied: true });
  api.chatJob.mockResolvedValue({ status: 'done', graph: generated, elapsed: 2 });
  const { user } = setup({ getGraph: () => current, onApply: g => { current = g; } });
  await send(user, 'Generate file workflow');
  await screen.findByText('✓ generated 1 nodes in 2s', {}, { timeout: 4000 });
  await send(user, 'Add cleaning');
  await screen.findByText('Added cleaning');
  const second = api.chatGraph.mock.calls[1][1];
  expect(second.graph).toEqual(generated);
  expect(second.history.some(m => m.content.includes('Applied changes: generated 1 nodes'))).toBe(true);
  await send(user, 'Delete that cleaning node');
  await screen.findByText('Removed cleaning');
  expect(api.chatGraph.mock.calls[2][1].graph).toEqual(expanded);
  expect(current).toEqual(generated);
});
it('tells the next turn about failed and undone operations', () => {
  const history = conversationHistory([{ role: 'assistant', content: 'Adding', errors: ['Mapping invalid'] }, { role: 'assistant', content: 'Changed', summary: 'added node', undone: true }]);
  expect(history[0].content).toContain('Operation failed: Mapping invalid');
  expect(history[1].content).toContain('subsequently undone');
});

it('keeps the completed reply and current identity when chat saves a rename', async () => {
  const renamed = { ...GRAPH, id: 'renamed' };
  api.chatGraph.mockResolvedValue({ reply: 'Renamed and saved', operations: [{ op: 'set_name' }, { op: 'save_graph' }], graph: renamed, saved: true, applied: true });
  const onApply = vi.fn(() => localStorage.setItem('evoagentx-studio:chat:renamed', '[]'));
  const { user } = setup({ getGraph: () => ({ ...GRAPH, id: 'g1' }), onApply });
  await send(user, 'Rename and save');
  await screen.findByText('Renamed and saved');
  const history = JSON.parse(localStorage.getItem('evoagentx-studio:chat:renamed'));
  expect(history.at(-1).content).toBe('Renamed and saved');
  expect(history.at(-1).snapshot.id).toBe('renamed');
});

it('opens workflow controls requested by the assistant', async () => {
 api.chatGraph.mockResolvedValue({ reply: 'Opening history', operations: [], panel: 'runs' });
 const onOpenPanel = vi.fn();
 const { user } = setup({ onOpenPanel });
 await send(user, 'Show recent runs');
 await waitFor(() => expect(onOpenPanel).toHaveBeenCalledWith('runs'));
});
it('resumes a historical canvas session without borrowing another session history',async()=>{
 api.chatGraph.mockImplementation(async(g,body)=>({reply:`Answer: ${body.message}`,operations:[]}));
 const {user}=setup();
 await send(user,'First workflow');
 await screen.findByText('Answer: First workflow');
 await user.click(screen.getByRole('button',{name:'New session'}));
 await send(user,'Second workflow');
 await screen.findByText('Answer: Second workflow');
 expect(api.chatGraph.mock.calls.at(-1)[1].history).toEqual([]);
 await user.click(screen.getByRole('button',{name:'History (2)'}));
 await user.click(screen.getByRole('button',{name:'Open session First workflow'}));
 await send(user,'Continue first workflow');
 await screen.findByText('Answer: Continue first workflow');
 const history=api.chatGraph.mock.calls.at(-1)[1].history;
 expect(history.some(m=>m.content==='First workflow')).toBe(true);
 expect(history.some(m=>m.content==='Second workflow')).toBe(false);
});
it('force stops a turn and never applies its late canvas changes',async()=>{
 let resolve;
 api.chatGraph.mockImplementationOnce(()=>new Promise(done=>{resolve=done;}));
 api.stopAssistant.mockResolvedValue({status:'stopping'});
 const {user,onApply}=setup();
 await send(user,'Slow change');
 await user.click(screen.getByRole('button',{name:'■ Stop'}));
 await screen.findByText('Stopped.');
 expect(api.stopAssistant).toHaveBeenCalledWith('g1',api.chatGraph.mock.calls.at(-1)[1].request_id);
 resolve({reply:'Late changes',applied:true,graph:GRAPH});
 await user.type(screen.getByRole('textbox'),'Can continue');
 expect(onApply).not.toHaveBeenCalled();
 expect(screen.queryByText('Late changes')).not.toBeInTheDocument();
 expect(screen.getByRole('button',{name:'Send'})).toBeEnabled();
});


describe('a run started from chat', () => {
  const seed = (runId) => localStorage.setItem('evoagentx-studio:chat:g1', JSON.stringify([
    { id: 'u1', role: 'user', content: 'run it' },
    { id: 'a1', role: 'assistant', content: 'ok', runStarted: runId }]));

  it('stops polling a run the server no longer has and releases the session controls', async () => {
    vi.useFakeTimers();
    try {
      seed('r-gone');
      api.runningChatJob.mockResolvedValue({});
      api.getRun.mockRejectedValue(Object.assign(new Error('HTTP 404'), { status: 404 }));
      render(<ChatPanel graphId="g1" getGraph={() => GRAPH} onApply={vi.fn()} onRunRequest={vi.fn()} />);
      await vi.advanceTimersByTimeAsync(20_000);
      expect(api.getRun).toHaveBeenCalledTimes(1);
      expect(screen.getByLabelText('New session')).not.toBeDisabled();
      expect(api.chatGraph).not.toHaveBeenCalled();     // nothing to diagnose
    } finally {
      vi.useRealTimers();
      localStorage.clear();
    }
  });

  it('stops polling on every settled state, not only a few', async () => {
    vi.useFakeTimers();
    try {
      seed('r-left');
      api.runningChatJob.mockResolvedValue({});
      api.chatGraph.mockReturnValue(new Promise(() => {}));
      api.getRun.mockResolvedValue({ run_id: 'r-left', status: 'abandoned' });
      render(<ChatPanel graphId="g1" getGraph={() => GRAPH} onApply={vi.fn()} onRunRequest={vi.fn()} />);
      await vi.advanceTimersByTimeAsync(20_000);
      expect(api.getRun).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
      localStorage.clear();
    }
  });
});

describe('a turn that outlives the panel', () => {
  beforeEach(() => {
    api.assistantTurn = vi.fn();
    api.runningAssistantTurns = vi.fn().mockResolvedValue({ turns: [] });
  });

  it('keeps running on the server when the panel closes and shows its result on return', async () => {
    const next = { name: 'demo', tasks: [{ name: 'summarise' }, { name: 'report' }] };
    api.chatGraph.mockImplementation(async (_g, body) => ({ turn_id: body.request_id, status: 'running' }));
    api.assistantTurn.mockResolvedValue({ status: 'running', elapsed: 3 });
    const { user, unmount } = setup();
    await send(user, 'Add a report step');
    expect(await screen.findByText('Thinking… (3s)')).toBeInTheDocument();
    const turnId = api.chatGraph.mock.calls[0][1].request_id;
    expect(api.chatGraph.mock.calls[0][1].background).toBe(true);
    unmount();   // e.g. the user opened the Inspector
    api.assistantTurn.mockResolvedValue({ turn_id: turnId, status: 'done', elapsed: 9,
      result: { reply: 'Added a reporting step.', operations: [{ op: 'add_node' }], applied: true, graph: next } });
    const { onApply } = setup();
    expect(await screen.findByText('Added a reporting step.')).toBeInTheDocument();
    expect(screen.getByText('Add a report step', { selector: '.chat-bubble' })).toBeInTheDocument();
    expect(api.assistantTurn).toHaveBeenLastCalledWith('g1', turnId);
    expect(onApply).toHaveBeenCalledWith(next);
    expect(screen.getByRole('button', { name: 'Undo this change' })).toBeInTheDocument();
    expect(localStorage.getItem('evoagentx-studio:chat:g1:pending-turn')).toBeNull();
    expect(screen.getByRole('button', { name: 'Send' })).toBeInTheDocument();
  });

  it('re-attaches to a running turn on remount and an explicit Stop still cancels it', async () => {
    localStorage.setItem('evoagentx-studio:chat:g1:pending-turn', JSON.stringify({ turnId: 'turn-1', sessionId: 'none', before: GRAPH }));
    api.assistantTurn.mockResolvedValue({ turn_id: 'turn-1', status: 'running', elapsed: 42 });
    api.stopAssistant.mockResolvedValue({ status: 'stopping' });
    const { user, onApply } = setup();
    expect(await screen.findByText('Thinking… (42s)')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '■ Stop' }));
    expect(await screen.findByText('Stopped.')).toBeInTheDocument();
    expect(api.stopAssistant).toHaveBeenCalledWith('g1', 'turn-1');
    expect(localStorage.getItem('evoagentx-studio:chat:g1:pending-turn')).toBeNull();
    expect(onApply).not.toHaveBeenCalled();
  });

  it('finds a running turn on the server when browser storage lost track of it', async () => {
    api.runningAssistantTurns.mockResolvedValue({ turns: [{ turn_id: 'turn-2', status: 'running', elapsed: 1 }] });
    api.assistantTurn.mockResolvedValue({ turn_id: 'turn-2', status: 'failed', error: 'LLM call failed: timeout' });
    setup();
    expect(await screen.findByText('LLM call failed: timeout')).toBeInTheDocument();
    expect(api.runningAssistantTurns).toHaveBeenCalledWith('g1', 'canvas');
  });
});

it('lets the user copy their own message and the reply', async () => {
  api.chatGraph.mockResolvedValue({ reply: 'Here is the answer', operations: [] });
  // user-event installs its own clipboard; read back what was written to it.
  const { user } = setup();
  await send(user, 'Please copy me');
  await screen.findByText('Here is the answer');
  await user.click(screen.getByRole('button', { name: 'Copy your message' }));
  expect(await navigator.clipboard.readText()).toBe('Please copy me');
  expect(await screen.findByText('Copied')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Copy reply' }));
  expect(await navigator.clipboard.readText()).toBe('Here is the answer');
});


describe('applied operations', () => {
  it('summarises what took effect, not what the model asked for', () => {
    expect(summarise(appliedOperations({
      operations: [{ op: 'delete_node', name: 'ghost' }, { op: 'validate' }],
      canvas_operations: [],
      agent_actions: [{ action: 'remove', id: 'a1' }],
    }))).toBe('removed Chat Agent · checked');
    // An older server without canvas_operations: the operations stand.
    expect(appliedOperations({ operations: [{ op: 'add_node' }] })).toEqual([{ op: 'add_node' }]);
  });
});

describe('a canvas edited while the turn ran', () => {
  const EDITED = { name: 'demo', tasks: [{ name: 'summarise' }, { name: 'mine' }] };
  const RESULT = { name: 'demo', tasks: [] };

  function racing(response) {
    let current = GRAPH;
    api.chatGraph.mockImplementation(async () => { current = EDITED; return response; });
    return setup({ getGraph: () => current });
  }

  it('replays the turn\'s edits onto the current canvas instead of replacing it', async () => {
    const replayed = { name: 'demo', tasks: [{ name: 'mine' }] };
    api.replayChatOperations.mockResolvedValue({ applied: true, graph: replayed, errors: [], notes: [] });
    const { onApply, user } = racing({ reply: 'Deleted.', operations: [{ op: 'delete_node', name: 'Summarise' }],
      canvas_operations: [{ op: 'delete_node', name: 'summarise' }], applied: true, graph: RESULT });

    await send(user, 'delete summarise');

    await waitFor(() => expect(onApply).toHaveBeenCalledWith(replayed));
    expect(api.replayChatOperations).toHaveBeenCalledWith('g1', { graph: EDITED, operations: [{ op: 'delete_node', name: 'summarise' }] });
    expect(onApply).not.toHaveBeenCalledWith(RESULT);
    expect(await screen.findByText(/applied on top of your changes/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Undo this change' }));
    expect(onApply).toHaveBeenLastCalledWith(EDITED);
  });

  it('keeps the person\'s edits when the turn\'s edits no longer apply', async () => {
    api.replayChatOperations.mockResolvedValue({ applied: false, graph: EDITED, errors: [], notes: ['delete_node: nothing matches'] });
    const { onApply, user } = racing({ reply: 'Deleted.', operations: [{ op: 'delete_node' }],
      canvas_operations: [{ op: 'delete_node', name: 'summarise' }], applied: true, graph: RESULT });

    await send(user, 'delete summarise');

    expect(await screen.findByText(/my edits were not applied, to keep yours/)).toBeInTheDocument();
    expect(onApply).not.toHaveBeenCalled();
    expect(screen.queryByText('✓ deleted')).not.toBeInTheDocument();
  });
});

describe('Chat Agent changes', () => {
  it('hands agent actions to the canvas and shows what failed', async () => {
    const onAgentActions = vi.fn().mockResolvedValue(["Chat Agent 'Helper' was not removed: busy"]);
    api.chatGraph.mockResolvedValue({ reply: 'Removed.', operations: [{ op: 'delete_agent', name: 'helper' }],
      canvas_operations: [], agent_actions: [{ action: 'remove', id: 'a1', name: 'Helper' }], applied: false });
    const { user } = setup({ onAgentActions });

    await send(user, 'delete the helper chat');

    await waitFor(() => expect(onAgentActions).toHaveBeenCalledWith([{ action: 'remove', id: 'a1', name: 'Helper' }]));
    expect(await screen.findByText("Chat Agent 'Helper' was not removed: busy")).toBeInTheDocument();
    expect(screen.getByText('✓ removed Chat Agent')).toBeInTheDocument();
  });
});

describe('a canvas edited while a generation ran', () => {
  const EDITED = { name: 'demo', tasks: [{ name: 'summarise' }, { name: 'mine' }] };
  const GENERATED = { name: 'demo', tasks: [{ name: 'fetch' }, { name: 'report' }] };

  function racing() {
    let current = GRAPH;
    api.chatGraph.mockImplementation(async () => { current = EDITED; return { reply: 'Generating', operations: [{ op: 'generate_workflow' }], job_id: 'race' }; });
    api.chatJob.mockResolvedValue({ status: 'done', graph: GENERATED, elapsed: 3 });
    return setup({ getGraph: () => current });
  }

  it('asks before replacing the canvas, and Undo brings the edited canvas back', async () => {
    const { onApply, user } = racing();
    await send(user, 'Generate a workflow');

    expect(await screen.findByText(/canvas changed while generating/, {}, { timeout: 4000 })).toBeInTheDocument();
    expect(onApply).not.toHaveBeenCalled();
    expect(screen.queryByText(/✓ generated/)).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Replace' }));
    expect(onApply).toHaveBeenCalledWith(GENERATED);
    expect(screen.getByText('✓ generated 2 nodes in 3s')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Replace' })).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Undo this change' }));
    expect(onApply).toHaveBeenLastCalledWith(EDITED);
  });

  it('keeps the person\'s canvas when they say so', async () => {
    const { onApply, user } = racing();
    await send(user, 'Generate a workflow');

    await user.click(await screen.findByRole('button', { name: 'Keep mine' }, { timeout: 4000 }));
    expect(onApply).not.toHaveBeenCalled();
    expect(screen.getByText(/Kept your canvas/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Replace' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Undo this change' })).not.toBeInTheDocument();
  });
});

describe('a generation that finishes onto a canvas nothing was lost from', () => {
  const GENERATED = { name: 'demo', tasks: [{ name: 'fetch' }, { name: 'report' }] };

  it('is applied straight away when the canvas was empty, whatever moved meanwhile', async () => {
    let current = { name: 'demo', tasks: [] };
    api.chatGraph.mockImplementation(async () => {
      current = { name: 'demo', tasks: [], memory_positions: { m: { x: 5, y: 5 } } };
      return { reply: 'Created', operations: [{ op: 'generate_workflow' }], job_id: 'empty' };
    });
    api.chatJob.mockResolvedValue({ status: 'done', graph: GENERATED, elapsed: 3 });
    const { onApply, user } = setup({ getGraph: () => current });
    await send(user, 'Create a new workflow');

    await waitFor(() => expect(onApply).toHaveBeenCalledWith(GENERATED), { timeout: 4000 });
    expect(screen.queryByRole('button', { name: 'Replace' })).not.toBeInTheDocument();
  });

  it('is applied when only node positions changed', async () => {
    const before = { name: 'demo', tasks: [{ name: 'old', x: 0, y: 0 }] };
    let current = before;
    api.chatGraph.mockImplementation(async () => {
      current = { name: 'demo', tasks: [{ name: 'old', x: 120, y: 40 }] };
      return { reply: 'Generating', operations: [{ op: 'generate_workflow' }], job_id: 'moved' };
    });
    api.chatJob.mockResolvedValue({ status: 'done', graph: GENERATED, elapsed: 3 });
    const { onApply, user } = setup({ getGraph: () => current });
    await send(user, 'Start over with a new workflow');

    await waitFor(() => expect(onApply).toHaveBeenCalledWith(GENERATED), { timeout: 4000 });
  });
});

describe('undoing Chat Agent changes', () => {
  it('hands the turn\'s agent undo to the canvas along with the workflow snapshot', async () => {
    const next = { name: 'demo', tasks: [] };
    const undoEntries = [{ action: 'remove', id: 'a1', name: 'Helper' }, { action: 'update', id: 'a2', name: 'Writer', previous: { name: 'Writer' } }];
    const onAgentActions = vi.fn().mockResolvedValue({ failed: [], undo: undoEntries });
    const onUndoAgentActions = vi.fn().mockResolvedValue([]);
    api.chatGraph.mockResolvedValue({ reply: 'Done.', operations: [{ op: 'delete_node' }, { op: 'delete_agent' }, { op: 'update_agent' }],
      canvas_operations: [{ op: 'delete_node', name: 'summarise' }], applied: true, graph: next,
      agent_actions: [{ action: 'remove', id: 'a1' }, { action: 'update', id: 'a2', patch: { name: 'Editor' } }] });
    const { onApply, user } = setup({ onAgentActions, onUndoAgentActions });

    await send(user, 'tidy up');
    await screen.findByText('Done.');
    expect(screen.queryByText(/Undo restores the workflow only/)).not.toBeInTheDocument();

    await user.click(await screen.findByRole('button', { name: 'Undo this change' }));
    expect(onApply).toHaveBeenLastCalledWith(GRAPH);
    await waitFor(() => expect(onUndoAgentActions).toHaveBeenCalledWith(undoEntries));
    expect(screen.getByText('Change undone.')).toBeInTheDocument();
  });

  it('offers Undo for a turn that only changed Chat Agents, and shows what could not be undone', async () => {
    const onAgentActions = vi.fn().mockResolvedValue({ failed: [], undo: [{ action: 'remove', id: 'a1', name: 'Helper' }] });
    const onUndoAgentActions = vi.fn().mockResolvedValue(["Chat Agent 'Helper' was not restored: gone"]);
    api.chatGraph.mockResolvedValue({ reply: 'Removed.', operations: [{ op: 'delete_agent' }], canvas_operations: [],
      agent_actions: [{ action: 'remove', id: 'a1' }], applied: false });
    const { onApply, user } = setup({ onAgentActions, onUndoAgentActions });

    await send(user, 'delete the helper chat');
    await user.click(await screen.findByRole('button', { name: 'Undo this change' }));

    expect(onApply).not.toHaveBeenCalled();
    expect(onUndoAgentActions).toHaveBeenCalledWith([{ action: 'remove', id: 'a1', name: 'Helper' }]);
    expect(await screen.findByText("Chat Agent 'Helper' was not restored: gone")).toBeInTheDocument();
  });
});
