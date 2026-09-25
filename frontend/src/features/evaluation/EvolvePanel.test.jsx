import { act, render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../api.js', () => ({
  api: {
    evolveMetrics: vi.fn(), evolvePresets: vi.fn(), getGraph: vi.fn(),
    previewEvolveResults: vi.fn(), listBatches: vi.fn(), listRuns: vi.fn(), startEvolveResults: vi.fn(), startEvolveUpload: vi.fn(), applyEvolve: vi.fn(),
    listEvolveTasks: vi.fn(), getEvolveTask: vi.fn(), stopEvolve: vi.fn(),
    graphEvaluators: vi.fn(), saveGraphEvaluators: vi.fn(), deleteGraphEvaluator: vi.fn(),
    previewGraphEvaluator: vi.fn(), runGraphEvaluator: vi.fn(), inspectEvaluatorCode: vi.fn(),
    evaluatorDraft: vi.fn(), saveEvaluatorDraft: vi.fn(), listDataResources: vi.fn(),
  },
}));
const { api } = await import('../../api.js');
import EvolvePanel, { NewTaskForm, TaskDetail } from './EvolvePanel.jsx';

beforeEach(() => {
  vi.clearAllMocks();
  api.previewEvolveResults.mockResolvedValue({matched_records: 458, available_records: 458});
  api.evolveMetrics.mockResolvedValue({ metrics: [{ name: 'exact_match', description: 'Exact', custom: false }, { name: 'f1', description: 'Token F1', custom: false }] });
  api.evolvePresets.mockResolvedValue({ presets: [
    { name: 'quick', label: 'Quick', blurb: 'minutes', num_candidates: 2, max_steps: 2 },
    { name: 'standard', label: 'Standard', blurb: 'usual', num_candidates: 4, max_steps: 4 }] });
  api.getGraph.mockResolvedValue({ tasks: [
    { name: 'input', kind: 'source' }, { name: 'detect' }, { name: 'decide' }, { name: 'match', kind: 'tool' }] });
  api.startEvolveUpload.mockResolvedValue({ task_id: 't9' });
  api.listBatches.mockResolvedValue([]);
  api.listRuns.mockResolvedValue([]);
  api.graphEvaluators.mockResolvedValue({ evaluators: [] });
  api.evaluatorDraft.mockResolvedValue({ code: '', config: {} });
  api.saveEvaluatorDraft.mockResolvedValue({ saved_at: '2026-01-01T00:00:00Z' });
  api.listDataResources.mockResolvedValue({ resources: [] });
});

const labelled = () => new File(['{"inputs":{"q":"a"},"label":"b"}\n'], 'labelled.jsonl', { type: 'application/json' });
async function withFile(view, user) {
  await user.upload(view.getByLabelText('File'), labelled());
}

describe('starting an optimization takes three decisions', () => {
  it('defaults to all LLM nodes, quick and exact_match for a labelled file', async () => {
    const onStarted = vi.fn();
    const { container } = render(<NewTaskForm initialSource="upload" initialMode="evolve_evaluate" graphId="g1" onStarted={onStarted} onError={vi.fn()} />);
    const view = within(container);
    const user = userEvent.setup();
    expect(await view.findByLabelText('decide')).toBeChecked();
    expect(view.getByLabelText('detect')).toBeChecked();
    expect(view.queryByLabelText('input')).not.toBeInTheDocument();     // sources are not prompts
    expect(view.queryByLabelText('Candidates')).not.toBeInTheDocument(); // behind Advanced
    expect(view.getByRole('button', { name: /start optimization/i })).toBeDisabled(); // no file yet
    await withFile(view, user);
    await user.click(view.getByRole('button', { name: /start optimization/i }));
    await waitFor(() => expect(api.startEvolveUpload).toHaveBeenCalledWith('g1', expect.any(File), expect.objectContaining({
      metric: 'exact_match', preset: 'quick', nodes: 'detect,decide' })));
    expect(onStarted).toHaveBeenCalledWith('t9');
  });

  it('offers only the generic data sources', async () => {
    const { container } = render(<NewTaskForm graphId="g1" onStarted={vi.fn()} onError={vi.fn()} />);
    const options = [...within(container).getByLabelText('Source').querySelectorAll('option')].map((o) => o.value);
    expect(options).toEqual(['canvas', 'saved_batch', 'saved_run', 'upload']);
    await waitFor(() => expect(api.getGraph).toHaveBeenCalled());
    expect(within(container).queryByLabelText('Dataset version')).not.toBeInTheDocument();
    expect(within(container).queryByLabelText('Split')).not.toBeInTheDocument();
  });

  it('lets a node be left alone and a preset be chosen', async () => {
    const { container } = render(<NewTaskForm initialSource="upload" initialMode="evolve_evaluate" graphId="g1" onStarted={vi.fn()} onError={vi.fn()} />);
    const view = within(container);
    const user = userEvent.setup();
    await user.click(await view.findByLabelText('detect'));
    await user.click(view.getByText('Standard'));
    await withFile(view, user);
    await user.click(view.getByRole('button', { name: /start optimization/i }));
    await waitFor(() => expect(api.startEvolveUpload).toHaveBeenCalledWith('g1', expect.any(File), expect.objectContaining({
      preset: 'standard', nodes: 'decide' })));
  });

  it('advanced numbers override the preset only when filled in', async () => {
    const { container } = render(<NewTaskForm initialSource="upload" initialMode="evolve_evaluate" graphId="g1" onStarted={vi.fn()} onError={vi.fn()} />);
    const view = within(container);
    const user = userEvent.setup();
    await view.findByLabelText('decide');
    await user.click(view.getByRole('button', { name: /advanced/i }));
    await user.type(view.getByLabelText('Rounds'), '6');
    await withFile(view, user);
    await user.click(view.getByRole('button', { name: /start optimization/i }));
    await waitFor(() => expect(api.startEvolveUpload).toHaveBeenCalled());
    const sent = api.startEvolveUpload.mock.calls[0][2];
    expect(sent.max_steps).toBe(6);
    expect(sent.num_candidates).toBeUndefined();
  });
});

describe('a finished optimization shows what changed and offers two ways to keep it', () => {
  const task = {
    task_id: 't1', status: 'done', metric: 'exact_match', elapsed_seconds: 600,
    params: { preset: 'quick', num_candidates: 2, max_steps: 2, n_train: 4, n_dev: 2 },
    baseline: { metrics: { score: 0.5 }, records: {} }, optimized: { metrics: { score: 0.75 }, records: {} },
    diff: [
      { name: 'detect', before: 'old detect', after: 'old detect', changed: false, optimized: true },
      { name: 'decide', before: 'old decide', after: 'NEW decide', changed: true, optimized: true },
    ],
  };

  it('shows the gain and only the prompts that changed', () => {
    const { container } = render(<TaskDetail task={task} onApplied={vi.fn()} />);
    const view = within(container);
    expect(view.getByText('+0.250')).toBeInTheDocument();
    expect(view.getByText('1 prompt changed')).toBeInTheDocument();
    expect(view.getByText('NEW decide')).toBeInTheDocument();
    expect(view.queryByText('old detect')).not.toBeInTheDocument();
  });

  it('applies in place through mode=replace', async () => {
    api.applyEvolve.mockResolvedValue({ id: 'g1', backup_id: 'g1-before' });
    const onApplied = vi.fn();
    const { container } = render(<TaskDetail task={task} onApplied={onApplied} />);
    await userEvent.setup().click(within(container).getByRole('button', { name: /apply to this workflow/i }));
    await waitFor(() => expect(api.applyEvolve).toHaveBeenCalledWith('t1', 'replace'));
    expect(onApplied).toHaveBeenCalledWith(expect.objectContaining({ backup_id: 'g1-before' }), 'replace');
  });
});

it('evaluation mode hides optimization controls and submits the file for scoring', async () => {
  const { container } = render(<NewTaskForm initialSource="upload" initialMode="evolve_evaluate" graphId="g1" onStarted={vi.fn()} onError={vi.fn()} />);
  const view = within(container);
  const user = userEvent.setup();
  await view.findByLabelText('decide');
  await user.click(view.getByLabelText('Evaluation only'));
  expect(view.queryByLabelText('decide')).not.toBeInTheDocument();
  await withFile(view, user);
  await user.click(view.getByRole('button', {name: 'Start evaluation'}));
  await waitFor(() => expect(api.startEvolveUpload).toHaveBeenCalledWith('g1', expect.any(File), expect.objectContaining({ mode: 'evaluate' })));
});

it('evaluation results show scores and records without apply actions', async () => {
  const { container } = render(<TaskDetail task={{ task_id: 'e1', status: 'done', params: {mode: 'evaluate', n_dev: 1}, baseline: {metrics: {score: 1}, records: {r1: {prediction: 'ok', label: 'ok', metrics: {score: 1}}}} }} onApplied={vi.fn()} />);
  const view = within(container);
  expect(view.getByText('1.000')).toBeInTheDocument();
  expect(view.queryByRole('button', {name: /apply to this workflow/i})).not.toBeInTheDocument();
  expect(view.queryByRole('button', {name: /save as new workflow/i})).not.toBeInTheDocument();
  await userEvent.setup().click(view.getByRole('button', {name: /show the judging records/i}));
  expect(view.getByText('r1')).toBeInTheDocument();
});


it('defaults to saved results and never submits a workflow rerun', async () => {
  api.listBatches.mockResolvedValue([{batch_id: 'b1', status: 'succeeded', total: 458}]);
  api.startEvolveResults.mockResolvedValue({task_id: 'saved1'});
  const {container} = render(<NewTaskForm graphId="g1" onStarted={vi.fn()} onError={vi.fn()} />);
  const view = within(container);
  await waitFor(() => expect(view.getByLabelText('Saved result')).toHaveValue('b1'));
  await userEvent.setup().click(view.getByRole('button', {name: 'Evaluate saved results'}));
  await waitFor(() => expect(api.startEvolveResults).toHaveBeenCalledWith('g1', expect.objectContaining({source: 'saved_batch', batch_id: 'b1', mode: 'evaluate'})));
  expect(api.startEvolveUpload).not.toHaveBeenCalled();
});


it('previews and submits saved results without dataset or split filters', async () => {
  api.listBatches.mockResolvedValue([{batch_id: 'b1', status: 'succeeded'}]);
  api.previewEvolveResults.mockResolvedValue({matched_records:178, available_records:178});
  api.startEvolveResults.mockResolvedValue({task_id:'plain'});
  const {container} = render(<NewTaskForm graphId="g1" onStarted={vi.fn()} onError={vi.fn()} />);
  const view=within(container); const user=userEvent.setup();
  await waitFor(()=>expect(view.getByLabelText('Saved result')).toHaveValue('b1'));
  expect(await view.findByText(/178 matching records/)).toBeInTheDocument();
  const previewed = api.previewEvolveResults.mock.calls.at(-1)[1];
  expect(previewed).not.toHaveProperty('dataset');
  expect(previewed).not.toHaveProperty('split');
  await user.click(view.getByRole('button',{name:'Evaluate saved results'}));
  await waitFor(()=>expect(api.startEvolveResults).toHaveBeenCalled());
  const sent = api.startEvolveResults.mock.calls[0][1];
  expect(sent).toMatchObject({source:'saved_batch', batch_id:'b1'});
  expect(sent).not.toHaveProperty('dataset');
  expect(sent).not.toHaveProperty('split');
});


it('uses the run ID even when a saved run also carries its batch ID', async () => {
  api.listRuns.mockResolvedValue([{run_id:'run-1',batch_id:'batch-1',status:'success'}]);
  api.startEvolveResults.mockResolvedValue({task_id:'single'});
  const {container}=render(<NewTaskForm initialSource="saved_run" graphId="g1" onStarted={vi.fn()} onError={vi.fn()} />);
  const view=within(container);
  await waitFor(()=>expect(view.getByLabelText('Saved result')).toHaveValue('run-1'));
  await waitFor(()=>expect(view.getByRole('button',{name:'Evaluate saved results'})).toBeEnabled());
  await userEvent.setup().click(view.getByRole('button',{name:'Evaluate saved results'}));
  await waitFor(()=>expect(api.startEvolveResults).toHaveBeenCalledWith('g1',expect.objectContaining({source:'saved_run',run_id:'run-1'})));
});


describe('stopping and ending a task early', () => {
  const running = { task_id: 'r1', status: 'running', stage: 'evaluating', params: { mode: 'evaluate', n_dev: 3 }, metric: 'exact_match' };

  it('stops a running task through the API', async () => {
    api.stopEvolve.mockResolvedValue({ stopping: true });
    const onStopped = vi.fn();
    const { container } = render(<TaskDetail task={running} onApplied={vi.fn()} onStopped={onStopped} />);
    const view = within(container);
    await userEvent.setup().click(view.getByRole('button', { name: 'Stop' }));
    await waitFor(() => expect(api.stopEvolve).toHaveBeenCalledWith('r1'));
    expect(onStopped).toHaveBeenCalledWith('r1');
    expect(view.getByRole('button', { name: 'Stopping…' })).toBeDisabled();
    // What Stop reaches, and what it cannot: the request already sent.
    expect(view.getByText(/A provider request already sent finishes on its own/)).toBeInTheDocument();
  });

  it('keeps Stop taken while the server reports the stop as requested', () => {
    const { container } = render(<TaskDetail task={{ ...running, stop_requested: true }} onApplied={vi.fn()} />);
    const view = within(container);
    expect(view.getByRole('button', { name: 'Stopping…' })).toBeDisabled();
    expect(view.getByText(/nothing it answers is applied/)).toBeInTheDocument();
  });

  it('reports a refusal to stop', async () => {
    api.stopEvolve.mockRejectedValue({ body: { detail: 'This task is not running.' } });
    const { container } = render(<TaskDetail task={running} onApplied={vi.fn()} />);
    await userEvent.setup().click(within(container).getByRole('button', { name: 'Stop' }));
    expect(await within(container).findByText('This task is not running.')).toBeInTheDocument();
  });

  it('shows stopped and interrupted tasks without a Stop button', () => {
    for (const status of ['stopped', 'interrupted']) {
      const { container, unmount } = render(<TaskDetail task={{ ...running, status }} onApplied={vi.fn()} />);
      const view = within(container);
      expect(view.queryByRole('button', { name: 'Stop' })).not.toBeInTheDocument();
      expect(view.getByText(status === 'stopped' ? /Stopped before it finished/ : /Interrupted/)).toBeInTheDocument();
      unmount();
    }
  });

  it('renders a report and extra metrics generically', () => {
    const task = { task_id: 'g1', status: 'done', params: { mode: 'evaluate', n_dev: 2 }, metric: 'canvas:judge',
      baseline: { metrics: { score: 0.5, recall: 0.25 }, report: { anything: 'goes' }, records: {},
        evaluations: { judge: { status: 'success', metrics: { accuracy: 0.5 }, objective: { metric: 'accuracy' } } } } };
    const { container } = render(<TaskDetail task={task} onApplied={vi.fn()} />);
    const view = within(container);
    expect(view.getByText('Evaluation report')).toBeInTheDocument();
    expect(view.getByText('Metrics')).toBeInTheDocument();
    expect(view.getByText(/judge · success · accuracy 0.5/)).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/trajectories|lead time|event cases/i);
  });
});

describe('the panel follows the workflow it is open on', () => {
  it('drops the previous workflow selection when the workflow changes', async () => {
    api.listEvolveTasks.mockImplementation(async (graphId) => graphId === 'A'
      ? [{ task_id: 'ta', graph_id: 'A', status: 'done', params: { mode: 'evaluate' }, baseline_score: 1 }] : []);
    api.getEvolveTask.mockResolvedValue({ task_id: 'ta', graph_id: 'A', status: 'done', params: { mode: 'evaluate', n_dev: 1 }, metric: 'exact_match',
      baseline: { metrics: { score: 1 }, records: {} } });
    const { rerender } = render(<EvolvePanel open graphId="A" onClose={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(await screen.findByText('Score 1.000'));
    expect(await screen.findByText(/ta · Evaluation only/)).toBeInTheDocument();
    rerender(<EvolvePanel open graphId="B" onClose={vi.fn()} />);
    await waitFor(() => expect(api.listEvolveTasks).toHaveBeenCalledWith('B'));
    await act(async () => {});
    expect(screen.queryByText(/ta · Evaluation only/)).not.toBeInTheDocument();
    expect(screen.queryByText('Score 1.000')).not.toBeInTheDocument();
  });

  it('marks stopped and interrupted tasks in the list', async () => {
    api.listEvolveTasks.mockResolvedValue([
      { task_id: 's1', status: 'stopped', params: { mode: 'evaluate' }, metric: 'm' },
      { task_id: 'i1', status: 'interrupted', params: { mode: 'evaluate' }, metric: 'm' }]);
    render(<EvolvePanel open graphId="A" onClose={vi.fn()} />);
    expect((await screen.findByText('stopped')).className).toContain('status-stopped');
    expect(screen.getByText('interrupted').className).toContain('status-stopped');
  });
});

describe('a canvas candidate replay reports each round', () => {
  const canvasTask = {
    task_id: 'c1', status: 'done', metric: 'canvas:quality',
    params: { mode: 'evolve_evaluate', nodes: ['detect'], n_dev: 3, source: { type: 'canvas' } },
    source: { type: 'canvas' },
    baseline: { metrics: { score: 0.5 }, objective: { metric: 'quality_score', direction: 'maximize' } },
    optimized: { metrics: { score: 0.8 } },
    candidates: [
      { round: 1, metrics: { score: 0.2 }, accepted: false, comparable: true, compared_with: 0.5, changed: ['detect'] },
      { round: 2, metrics: { score: 0.8 }, accepted: true, comparable: true, compared_with: 0.5, changed: ['detect'] },
      { round: 3, metrics: { score: 0.9 }, accepted: false, comparable: false, compared_with: 0.8, changed: ['detect'] },
    ],
    diff: [{ name: 'detect', before: 'old', after: 'new', changed: true }],
  };

  it('shows every round, what it was compared with and why it was not kept', () => {
    const { container } = render(<TaskDetail task={canvasTask} onApplied={vi.fn()} onStopped={vi.fn()} />);
    const rows = [...container.querySelectorAll('.evolve-candidates tbody tr')].map((r) => r.textContent);
    expect(rows).toHaveLength(3);
    expect(rows[0]).toContain('rejected');
    expect(rows[1]).toContain('kept');
    expect(rows[2]).toContain('not comparable');
    expect(rows[1]).toContain('0.800');
  });

  it('names the record it is on while a replay is running', () => {
    const { container } = render(<TaskDetail task={{ ...canvasTask, status: 'running', stage: 'candidate-2: record 2/3' }} onApplied={vi.fn()} onStopped={vi.fn()} />);
    expect(within(container).getByText('candidate-2: record 2/3')).toBeInTheDocument();
  });
});

describe('labels stay out of the proposal prompt unless asked for', () => {
  it('starts a canvas evolution without sharing labels, and with them when ticked', async () => {
    api.getGraph.mockResolvedValue({ tasks: [{ name: 'detect' }] });
    api.graphEvaluators.mockResolvedValue({ evaluators: [{ name: 'quality', metric: 'accuracy', enabled: true }] });
    api.startEvolveResults.mockResolvedValue({ task_id: 'c2' });
    const { container } = render(<NewTaskForm initialSource="canvas" initialMode="evolve_evaluate" graphId="g1" onStarted={vi.fn()} onError={vi.fn()} />);
    const view = within(container);
    const user = userEvent.setup();
    await user.selectOptions(await view.findByLabelText('Objective evaluator'), 'quality');
    await user.click(view.getByRole('button', { name: /start optimization/i }));
    await waitFor(() => expect(api.startEvolveResults).toHaveBeenCalledWith('g1', expect.objectContaining({ share_labels: false })));
    await user.click(view.getByLabelText(/show expected answers/i));
    await user.click(view.getByRole('button', { name: /start optimization/i }));
    await waitFor(() => expect(api.startEvolveResults).toHaveBeenLastCalledWith('g1', expect.objectContaining({ share_labels: true })));
  });
});


describe('the objective comes from the workflow\'s saved evaluators', () => {
  it('lists them with their metric, never a canvas node', async () => {
    api.getGraph.mockResolvedValue({ tasks: [{ name: 'detect' }, { name: 'judge', kind: 'evaluator' }] });
    api.graphEvaluators.mockResolvedValue({ evaluators: [
      { name: 'quality', metric: 'accuracy', enabled: true },
      { name: 'old', metric: 'f1', enabled: false }] });
    api.listBatches.mockResolvedValue([{ batch_id: 'b1', status: 'succeeded' }]);
    const { container } = render(<NewTaskForm graphId="g1" onStarted={vi.fn()} onError={vi.fn()} />);
    const view = within(container);
    const options = [...(await view.findByLabelText('Objective evaluator')).querySelectorAll('option')].map(o => o.textContent);
    expect(options).toEqual(['Use existing metric', 'quality · accuracy']);   // disabled ones are not offered
    await userEvent.setup().selectOptions(view.getByLabelText('Objective evaluator'), 'quality');
    await waitFor(() => expect(api.previewEvolveResults).toHaveBeenLastCalledWith('g1', expect.objectContaining({ evaluator: 'quality' })));
    expect(container.textContent).not.toMatch(/Canvas evaluator/);
  });

  it('says so when the workflow has no saved evaluator yet', async () => {
    api.listBatches.mockResolvedValue([{ batch_id: 'b1', status: 'succeeded' }]);
    const { container } = render(<NewTaskForm graphId="g1" onStarted={vi.fn()} onError={vi.fn()} />);
    expect(await within(container).findByTestId('no-saved-evaluators')).toHaveTextContent('No saved evaluators');
  });
});

describe('the panel is where evaluation is written', () => {
  it('opens the evaluator editor from the sidebar', async () => {
    api.listEvolveTasks.mockResolvedValue([]);
    render(<EvolvePanel open graphId="g1" onClose={vi.fn()} />);
    await userEvent.setup().click(await screen.findByRole('button', { name: 'Write evaluator' }));
    expect(await screen.findByLabelText('Python Evaluator code')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Check code' })).toBeInTheDocument();
  });
});
