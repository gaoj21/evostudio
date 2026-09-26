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
  // The workflow's evaluation code, kept from Evaluate.
  api.graphEvaluators.mockResolvedValue({ evaluators: [{ name: 'evaluation', metric: 'score', direction: 'maximize', enabled: true }] });
  api.startEvolveResults.mockResolvedValue({ task_id: 't9' });
  api.evaluatorDraft.mockResolvedValue({ code: '', config: {} });
  api.saveEvaluatorDraft.mockResolvedValue({ saved_at: '2026-01-01T00:00:00Z' });
  api.listDataResources.mockResolvedValue({ resources: [] });
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

it('evaluation results show scores and records without apply actions', async () => {
  const { container } = render(<TaskDetail task={{ task_id: 'e1', status: 'done', params: {mode: 'evaluate', n_dev: 1}, baseline: {metrics: {score: 1}, records: {r1: {prediction: 'ok', label: 'ok', metrics: {score: 1}}}} }} onApplied={vi.fn()} />);
  const view = within(container);
  expect(view.getByText('1.000')).toBeInTheDocument();
  expect(view.queryByRole('button', {name: /apply to this workflow/i})).not.toBeInTheDocument();
  expect(view.queryByRole('button', {name: /save as new workflow/i})).not.toBeInTheDocument();
  await userEvent.setup().click(view.getByRole('button', {name: /show the judging records/i}));
  expect(view.getByText('r1')).toBeInTheDocument();
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

describe('evolve holds out whole entities for validation', () => {
  it('shows dev before/after and warns when the held-out score did not improve', () => {
    const task = { task_id: 'h1', status: 'done', params: { mode: 'evolve_evaluate', n_dev: 20, source: { type: 'canvas' } },
      baseline: { metrics: { score: 0 }, objective: { direction: 'maximize' } }, optimized: { metrics: { score: 1 } },
      split: { unit: 'trajectory', dev_units: 7, val_units: 3, dev_records: 14, val_records: 6, seed: 0, val_fraction: 0.3 },
      validation: { unit: 'trajectory', val_units: 3, val_records: 6, seed: 0, val_fraction: 0.3,
        baseline: { score: 1 }, optimized: { score: 0 }, improved: false, changed: true },
      diff: [], candidates: [] };
    const { container } = render(<TaskDetail task={task} onApplied={vi.fn()} />);
    expect(container.textContent).toMatch(/on dev: 7 trajectories \(14 records\)/);
    const v = within(container).getByTestId('validation');
    expect(v.textContent).toMatch(/on 3 trajectories \(6 records\)/);
    expect(v.textContent).toMatch(/did not score better on the held-out data/);
  });
});

it('says what val was: the latest values of the chosen field', () => {
  const task = { task_id: 'h2', status: 'done', params: { mode: 'evolve_evaluate', source: { type: 'canvas' } },
    baseline: { metrics: { score: 0.5 } }, optimized: { metrics: { score: 0.7 } },
    split: { unit: 'as_of', field: 'as_of', order: 'latest', dev_units: 3, val_units: 2, dev_records: 12, val_records: 8 },
    validation: { unit: 'as_of', field: 'as_of', order: 'latest', val_units: 2, val_records: 8, val_fraction: 0.4,
      baseline: { score: 0.5 }, optimized: { score: 0.6 }, improved: true, changed: true }, diff: [], candidates: [] };
  const { container } = render(<TaskDetail task={task} onApplied={vi.fn()} />);
  expect(container.textContent).toMatch(/on dev: 3 as_of values \(12 records\)/);
  expect(within(container).getByTestId('validation').textContent).toMatch(/on 2 as_of values \(8 records\).*latest 40% of the as_of values/);
});
