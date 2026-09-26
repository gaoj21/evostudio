import { render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../api.js', () => ({ api: {
  getGraph: vi.fn(), listEvolveTasks: vi.fn(), getEvolveTask: vi.fn(), listBatches: vi.fn(), listRuns: vi.fn(),
  previewEvolveResults: vi.fn(), startEvolveResults: vi.fn(), stopEvolve: vi.fn(), applyEvolve: vi.fn(),
  evaluatorDraft: vi.fn(), saveEvaluatorDraft: vi.fn(), inspectEvaluatorCode: vi.fn(), listDataResources: vi.fn(),
} }));
const { api } = await import('../../api.js');
import EvolvePanel, { NewTaskForm } from './EvolvePanel.jsx';

const CODE = 'def evaluate(records, field: str = "answer"):\n    return {"metrics": {"accuracy": 1, "recall": 0.5}}\n';
const EVALUATION = { task_id: 'e1', status: 'done', created_at: '2026-09-01T10:00:00Z', baseline_score: 0.8,
  params: { mode: 'evaluate', evaluator_entry: { code: CODE, metric: '', direction: 'maximize' } } };
const EVALUATION_DETAIL = { ...EVALUATION, baseline: { metrics: { score: 0.8 },
  evaluations: { evaluation: { status: 'success', metrics: { accuracy: 0.8, recall: 0.5 } } } } };

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  api.getGraph.mockResolvedValue({ tasks: [
    { name: 'feed', kind: 'source', outputs: [{ name: 'company' }, { name: 'as_of' }] }, { name: 'detect' }, { name: 'decide' }] });
  api.listEvolveTasks.mockResolvedValue([EVALUATION, { task_id: 'x', status: 'done', params: { mode: 'evolve_evaluate' } }]);
  api.getEvolveTask.mockResolvedValue(EVALUATION_DETAIL);
  api.listBatches.mockResolvedValue([{ batch_id: 'b1', status: 'succeeded', total: 12 }]);
  api.listRuns.mockResolvedValue([{ run_id: 'r1', batch_id: 'b9', status: 'success' }]);
  api.previewEvolveResults.mockResolvedValue({ matched_records: 12 });
  api.startEvolveResults.mockResolvedValue({ task_id: 't9' });
  api.evaluatorDraft.mockResolvedValue({ code: '', config: {} });
  api.saveEvaluatorDraft.mockResolvedValue({});
  api.inspectEvaluatorCode.mockResolvedValue({ kind: 'function', params: [{ name: 'field', type: 'str', required: false, default: 'answer' }] });
  api.listDataResources.mockResolvedValue({ resources: [] });
});

async function paste(view, user, code = CODE) {
  const editor = await view.findByLabelText('Python Evaluator code');
  await user.clear(editor);
  await user.paste(code);
  await user.click(view.getByRole('button', { name: 'Confirm code' }));
}

describe('an evaluation is code brought to the data', () => {
  it('evaluates pasted code on a saved batch, with the parameters the code declares', async () => {
    const onStarted = vi.fn();
    const { container } = render(<NewTaskForm graphId="g1" onStarted={onStarted} onError={vi.fn()} />);
    const view = within(container);
    const user = userEvent.setup();
    const go = () => view.getByRole('button', { name: 'Evaluate saved results' });
    await waitFor(() => expect(view.getByLabelText('Saved result')).toHaveValue('b1'));
    expect(go()).toBeDisabled();                                  // no code yet
    await paste(view, user);
    await waitFor(() => expect(go()).toBeEnabled());
    await user.click(go());
    await waitFor(() => expect(api.startEvolveResults).toHaveBeenCalledWith('g1', expect.objectContaining({
      source: 'saved_batch', batch_id: 'b1', mode: 'evaluate', code: CODE, config: { field: 'answer' }, timeout: 120 })));
    expect(onStarted).toHaveBeenCalledWith('t9');
    expect(view.queryByLabelText('Optimize')).toBeNull();          // nothing to optimize in an evaluation
  });

  it('can replay the canvas Input instead, and a saved run is evaluated by its run id', async () => {
    const { container } = render(<NewTaskForm graphId="g1" onStarted={vi.fn()} onError={vi.fn()} />);
    const view = within(container);
    const user = userEvent.setup();
    await paste(view, user);
    await user.selectOptions(view.getByLabelText('Source'), 'saved_run');
    await waitFor(() => expect(view.getByLabelText('Saved result')).toHaveValue('r1'));
    await waitFor(() => expect(view.getByRole('button', { name: 'Evaluate saved results' })).toBeEnabled());
    await user.click(view.getByRole('button', { name: 'Evaluate saved results' }));
    await waitFor(() => expect(api.startEvolveResults).toHaveBeenLastCalledWith('g1', expect.objectContaining({ source: 'saved_run', run_id: 'r1' })));
    await user.selectOptions(view.getByLabelText('Source'), 'canvas');
    await user.click(view.getByRole('button', { name: 'Start evaluation' }));
    await waitFor(() => expect(api.startEvolveResults).toHaveBeenLastCalledWith('g1', expect.objectContaining({
      source: 'canvas', mode: 'evaluate', code: CODE, workers: 4 })));
  });
});

describe('an Evolve continues an evaluation', () => {
  it('optimizes a metric of the chosen evaluation, replaying the canvas Input', async () => {
    const { container } = render(<NewTaskForm graphId="g1" initialMode="evolve" onStarted={vi.fn()} onError={vi.fn()} />);
    const view = within(container);
    const user = userEvent.setup();
    const from = await view.findByLabelText('Evaluation to continue');
    await waitFor(() => expect([...from.querySelectorAll('option')].map((o) => o.value)).toEqual(['e1']));   // finished evaluations only
    await waitFor(() => expect([...view.getByLabelText('Optimize').querySelectorAll('option')].map((o) => o.value)).toEqual(['accuracy', 'recall']));
    expect(view.queryByLabelText('Python Evaluator code')).toBeNull();                   // the code is the evaluation's
    await user.selectOptions(view.getByLabelText('Optimize'), 'recall');
    await user.click(view.getByLabelText('detect'));
    await user.selectOptions(view.getByLabelText('Split dev / val by'), 'as_of');
    await user.selectOptions(view.getByLabelText('Held-out values'), 'latest');
    await user.click(view.getByRole('button', { name: 'Start evolution' }));
    await waitFor(() => expect(api.startEvolveResults).toHaveBeenCalledWith('g1', {
      source: 'canvas', mode: 'evolve', from_evaluation: 'e1', metric: 'recall', direction: 'maximize', nodes: ['decide'],
      rounds: 1, share_labels: false, workers: 4, val_fraction: 0.3, split_field: 'as_of', split_order: 'latest' }));
  });

  it('asks for an evaluation first when there is none', async () => {
    api.listEvolveTasks.mockResolvedValue([]);
    const { container } = render(<NewTaskForm graphId="g1" initialMode="evolve" onStarted={vi.fn()} onError={vi.fn()} />);
    const view = within(container);
    expect(await view.findByTestId('no-evaluation')).toHaveTextContent('Run an Evaluation first');
    expect(view.getByRole('button', { name: 'Start evolution' })).toBeDisabled();
  });
});

describe('the panel', () => {
  it('has no separate evaluation code page, and a finished evaluation offers to evolve from it', async () => {
    api.listEvolveTasks.mockResolvedValue([EVALUATION]);
    const { container } = render(<EvolvePanel open graphId="g1" onClose={vi.fn()} />);
    const user = userEvent.setup();
    expect(screen.queryByRole('button', { name: 'Evaluation code' })).toBeNull();
    await waitFor(() => expect(container.querySelector('.evolve-task')).not.toBeNull());
    await user.click(container.querySelector('.evolve-task'));
    await user.click(await screen.findByRole('button', { name: 'Evolve from this evaluation' }));
    expect(await screen.findByLabelText('Evaluation to continue')).toHaveValue('e1');
    expect(screen.getByLabelText('Evolve')).toBeChecked();
  });
});
