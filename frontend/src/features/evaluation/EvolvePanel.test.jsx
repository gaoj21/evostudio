import { render, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../api.js', () => ({
  api: {
    evolveMetrics: vi.fn(), evolvePresets: vi.fn(), creditRiskSource: vi.fn(), getGraph: vi.fn(),
    previewEvolveResults: vi.fn(), listBatches: vi.fn(), listRuns: vi.fn(), startEvolveResults: vi.fn(), startEvolveSource: vi.fn(), startEvolveUpload: vi.fn(), applyEvolve: vi.fn(),
    listEvolveTasks: vi.fn(), getEvolveTask: vi.fn(),
  },
}));
const { api } = await import('../../api.js');
import { NewTaskForm, TaskDetail } from './EvolvePanel.jsx';

beforeEach(() => {
  api.previewEvolveResults.mockResolvedValue({matched_records: 458, matched_cases: 107, missing_cases: 0});
  api.evolveMetrics.mockResolvedValue({ metrics: [{ name: 'exact_match' }, { name: 'credit_risk' }] });
  api.evolvePresets.mockResolvedValue({ presets: [
    { name: 'quick', label: 'Quick', blurb: 'minutes', num_candidates: 2, max_steps: 2 },
    { name: 'standard', label: 'Standard', blurb: 'usual', num_candidates: 4, max_steps: 4 }] });
  api.creditRiskSource.mockResolvedValue({ splits: { train: 28, test: 7 } });
  api.getGraph.mockResolvedValue({ tasks: [
    { name: 'feed', kind: 'source' }, { name: 'detect' }, { name: 'decide' }, { name: 'match', kind: 'tool' }] });
  api.startEvolveSource.mockResolvedValue({ task_id: 't9' });
});

describe('starting an optimization takes three decisions', () => {
  it('defaults to the credit-risk data, all LLM nodes, quick, credit_risk metric', async () => {
    const onStarted = vi.fn();
    const { container } = render(<NewTaskForm initialSource="credit_risk" initialMode="evolve_evaluate" graphId="g1" onStarted={onStarted} onError={vi.fn()} />);
    const view = within(container);
    expect(await view.findByLabelText('decide')).toBeChecked();
    expect(view.getByLabelText('detect')).toBeChecked();
    expect(view.queryByLabelText('feed')).not.toBeInTheDocument();     // sources are not prompts
    expect(view.queryByLabelText('Candidates')).not.toBeInTheDocument(); // behind Advanced
    await userEvent.setup().click(view.getByRole('button', { name: /start optimization/i }));
    await waitFor(() => expect(api.startEvolveSource).toHaveBeenCalledWith('g1', expect.objectContaining({
      metric: 'credit_risk', preset: 'quick', split: 'dev', n: 6, nodes: ['detect', 'decide'] })));
    expect(onStarted).toHaveBeenCalledWith('t9');
  });

  it('lets a node be left alone and a preset be chosen', async () => {
    const { container } = render(<NewTaskForm initialSource="credit_risk" initialMode="evolve_evaluate" graphId="g1" onStarted={vi.fn()} onError={vi.fn()} />);
    const view = within(container);
    const user = userEvent.setup();
    await user.click(await view.findByLabelText('detect'));
    await user.click(view.getByText('Standard'));
    await user.click(view.getByRole('button', { name: /start optimization/i }));
    await waitFor(() => expect(api.startEvolveSource).toHaveBeenCalledWith('g1', expect.objectContaining({
      preset: 'standard', nodes: ['decide'] })));
  });

  it('advanced numbers override the preset only when filled in', async () => {
    const { container } = render(<NewTaskForm initialSource="credit_risk" initialMode="evolve_evaluate" graphId="g1" onStarted={vi.fn()} onError={vi.fn()} />);
    const view = within(container);
    const user = userEvent.setup();
    await view.findByLabelText('decide');
    await user.click(view.getByRole('button', { name: /advanced/i }));
    await user.type(view.getByLabelText('Rounds'), '6');
    await user.click(view.getByRole('button', { name: /start optimization/i }));
    await waitFor(() => expect(api.startEvolveSource).toHaveBeenCalled());
    const sent = api.startEvolveSource.mock.calls[0][1];
    expect(sent.max_steps).toBe(6);
    expect(sent.num_candidates).toBeUndefined();
  });
});

describe('a finished optimization shows what changed and offers two ways to keep it', () => {
  const task = {
    task_id: 't1', status: 'done', metric: 'credit_risk', elapsed_seconds: 600,
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

it('lets Evolve choose a dataset version and split and sends both', async () => {
  const version='2026-09-10-random-dev-test-v1';
  api.creditRiskSource.mockImplementation(async dataset => ({
    datasets:[{id:'contemporary',label:'Legacy'},{id:version,label:'Random 60:40'}],
    splits:dataset===version?{dev:64,test:43}:{dev:25,test:16},
    ...(dataset===version?{evolve_splits:{dev:30,test:23},evolve_note:'Only eligible labelled cases are used.'}:{})
  }));
  api.startEvolveSource.mockResolvedValue({task_id:'picked'});
  const {container}=render(<NewTaskForm initialSource="credit_risk" initialMode="evolve_evaluate" graphId="g1" onStarted={vi.fn()} onError={vi.fn()}/>);
  const view=within(container),user=userEvent.setup();
  await view.findByRole('option',{name:'Random 60:40'});
  await user.selectOptions(view.getByLabelText('Dataset version'),version);
  await view.findByText(/30 eligible cases/);
  await user.selectOptions(view.getByLabelText('Split'),'test');
  await view.findByText(/23 eligible cases/);
  await user.click(view.getByRole('button',{name:'Start optimization'}));
  await waitFor(()=>expect(api.startEvolveSource).toHaveBeenLastCalledWith('g1',expect.objectContaining({dataset:version,split:'test'})));
});


it('evaluation mode submits one record and hides optimization controls', async () => {
  const { container } = render(<NewTaskForm initialSource="credit_risk" initialMode="evolve_evaluate" graphId="g1" onStarted={vi.fn()} onError={vi.fn()} />);
  const view = within(container);
  const user = userEvent.setup();
  await view.findByLabelText('decide');
  await user.click(view.getByLabelText('Evaluation only'));
  expect(view.queryByLabelText('decide')).not.toBeInTheDocument();
  await user.clear(view.getByLabelText('Samples'));
  await user.type(view.getByLabelText('Samples'), '1');
  await user.click(view.getByRole('button', {name: 'Start evaluation'}));
  await waitFor(() => expect(api.startEvolveSource).toHaveBeenCalledWith('g1', expect.objectContaining({ mode: 'evaluate', n: 1 })));
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
  expect(api.startEvolveSource).not.toHaveBeenCalled();
});


it('filters saved results by dataset and test split before submitting', async () => {
  api.listBatches.mockResolvedValue([{batch_id: 'b1', status: 'succeeded'}]);
  api.creditRiskSource.mockResolvedValue({datasets: [{id:'release-v1',label:'Release v1'}], splits:{dev:64,test:43}});
  api.previewEvolveResults.mockResolvedValue({matched_records:178,matched_cases:43,missing_cases:0});
  api.startEvolveResults.mockResolvedValue({task_id:'filtered'});
  const {container} = render(<NewTaskForm graphId="g1" onStarted={vi.fn()} onError={vi.fn()} />);
  const view=within(container); const user=userEvent.setup();
  await waitFor(()=>expect(view.getByLabelText('Saved result')).toHaveValue('b1'));
  await user.selectOptions(view.getByLabelText('Dataset version'),'release-v1');
  await user.selectOptions(view.getByLabelText('Split'),'test');
  await waitFor(()=>expect(api.previewEvolveResults).toHaveBeenLastCalledWith('g1',expect.objectContaining({dataset:'release-v1',split:'test',batch_id:'b1'})));
  await waitFor(()=>expect(view.getByRole('button',{name:'Evaluate saved results'})).toBeEnabled());
  await user.click(view.getByRole('button',{name:'Evaluate saved results'}));
  await waitFor(()=>expect(api.startEvolveResults).toHaveBeenCalledWith('g1',expect.objectContaining({dataset:'release-v1',split:'test'})));
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
