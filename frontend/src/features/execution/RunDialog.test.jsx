/**
 * The batch tab of the run dialog.
 *
 * Two things made it unpleasant: it asked for a file even when the graph
 * already said where its data comes from, and it never said how many runs you
 * were about to start — so `n=4` with monthly stepping quietly meant 24.
 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import RunDialog, { parseValue, previewSummary } from './RunDialog.jsx';

vi.mock('../../api.js', () => ({
  api: {
    runPlan: vi.fn(),
    runGraph: vi.fn(),
    creditRiskSource: vi.fn(),
    listMetrics: vi.fn(),
    listCustomTools: vi.fn(),
    previewBatchCanvas: vi.fn(),
    previewBatchSource: vi.fn(),
    previewBatchUpload: vi.fn(),
    runBatchCanvas: vi.fn(),
    collectSource: vi.fn(),
    sourceCollection: vi.fn(),
    stopSourceCollection: vi.fn(),
    runBatchSource: vi.fn(),
    runBatchUpload: vi.fn(),
  },
}));

const { api } = await import('../../api.js');

// What POST /run-plan answers: schema, start points, warnings and prefill,
// all from one graph revision, identified by plan_id.
const PLAN = {
  plan_id: 'plan:aaaa', graph_revision: 'rev:1', mode: 'single', start_at: [],
  nodes: [{ name: 'a' }, { name: 'b' }], inputs: [], start_points: ['a', 'b'],
  source: null, warnings: [], prefill: {}, prefill_from_run: null, prefill_missing: [],
};

const STEPPED = {
  total: 24, samples: 4, steps: 6, steps_min: 6,
  dates: ['2025-03-31', '2025-08-31'], fields: ['company', 'news_batch'],
};

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();   // last-used inputs would prefill the next test
  api.runPlan.mockResolvedValue(PLAN);
  api.creditRiskSource.mockResolvedValue({ splits: { test: 41 }, fields: ['company'] });
  api.listMetrics.mockResolvedValue({ metrics: [] });
  api.listCustomTools.mockResolvedValue({ tools: [] });
  api.previewBatchCanvas.mockResolvedValue(STEPPED);
  api.previewBatchSource.mockResolvedValue({ total: 3, samples: 3, steps: 1, steps_min: 1 });
});

function open({ hasCanvasSource = true, beforeRun, onSubmit } = {}) {
  const submit = onSubmit || vi.fn().mockResolvedValue({ ok: true, runId: 'run-1' });
  render(
    <RunDialog
      open
      graphId="g1"
      hasCanvasSource={hasCanvasSource}
      onCancel={vi.fn()}
      onSubmit={submit}
      beforeRun={beforeRun}
      onBatchStart={vi.fn()}
    />
  );
  return Object.assign(userEvent.setup(), { submit });
}

// The dialog opens on the single-run tab; the batch form is behind it.
async function batchTab(user) {
  await user.click(await screen.findByRole('button', { name: /batch/i }));
}

describe('previewSummary', () => {
  it('says nothing clever when each record is its own run', () => {
    expect(previewSummary({ total: 4, samples: 4, steps: 1, steps_min: 1 }))
      .toBe('4 runs');
  });

  it('spells out that stepping multiplied the batch', () => {
    // 4 samples, 6 dates each. The number you typed was 4.
    expect(previewSummary(STEPPED)).toBe(
      '24 runs — 4 samples stepped over 6 dates each covering 2025-03-31 to 2025-08-31'
    );
  });

  it('reports a range when windows differ in length', () => {
    expect(previewSummary({ ...STEPPED, steps_min: 3 }))
      .toContain('3–6 dates each');
  });

  it('gets the singular right', () => {
    expect(previewSummary({ total: 1, samples: 1, steps: 1, steps_min: 1 }))
      .toBe('1 run');
  });

  it('has nothing to say before the count arrives', () => {
    expect(previewSummary(null)).toBeNull();
  });
});

describe('batch data source', () => {
  it('starts on the canvas source when the graph has one', async () => {
    // Asking for a file when the graph already declares its own feed is the
    // wrong question, and it was the default.
    const user = open();
    await batchTab(user);

    expect(await screen.findByLabelText(/data source/i)).toHaveValue('canvas');
    await waitFor(() => expect(api.previewBatchCanvas).toHaveBeenCalledWith('g1', expect.anything()));
  });

  it('still asks for a file when there is no source node', async () => {
    const user = open({ hasCanvasSource: false });
    await batchTab(user);

    expect(await screen.findByLabelText(/data source/i)).toHaveValue('upload');
    expect(api.previewBatchCanvas).not.toHaveBeenCalled();
  });

  it('saves the canvas first, so the count reflects what is on screen', async () => {
    // The source node's n / step live in the graph; counting the saved copy
    // of an edited canvas would report the old numbers.
    const beforeRun = vi.fn().mockResolvedValue();
    const user = open({ beforeRun });
    await batchTab(user);

    await waitFor(() => expect(beforeRun).toHaveBeenCalled());
    await waitFor(() => expect(api.previewBatchCanvas).toHaveBeenCalled());
  });

  it('previews with the new graph id when saving renamed it', async () => {
    const beforeRun = vi.fn().mockResolvedValue({ id: 'renamed-graph' });
    const user = open({ beforeRun });
    await batchTab(user);

    await waitFor(() => expect(api.previewBatchCanvas).toHaveBeenCalledWith('renamed-graph', expect.anything()));
  });
});

describe('how big this batch is', () => {
  it('shows the run count before you start', async () => {
    const user = open();
    await batchTab(user);

    expect(await screen.findByText(/24 runs/)).toBeInTheDocument();
  });

  it('puts the count on the button you are about to press', async () => {
    const user = open();
    await batchTab(user);

    expect(await screen.findByRole('button', { name: 'Run batch (24)' })).toBeInTheDocument();
  });

  it('warns when the batch is a large one', async () => {
    const user = open();
    await batchTab(user);

    expect(await screen.findByText(/This one is large/)).toBeInTheDocument();
  });

  it('does not warn about a small one', async () => {
    api.previewBatchCanvas.mockResolvedValue({ total: 4, samples: 4, steps: 1, steps_min: 1 });
    const user = open();
    await batchTab(user);

    await screen.findByText(/4 runs/);
    expect(screen.queryByText(/This one is large/)).not.toBeInTheDocument();
  });

  it('names the fields each run receives', async () => {
    const user = open();
    await batchTab(user);

    expect(await screen.findByText(/company, news_batch/)).toBeInTheDocument();
  });

  it('reports a source it cannot read instead of a count', async () => {
    api.previewBatchCanvas.mockRejectedValue({ body: { detail: 'no connected source node' } });
    const user = open();
    await batchTab(user);

    expect(await screen.findByText(/no connected source node/)).toBeInTheDocument();
    expect(screen.queryByText(/runs$/)).not.toBeInTheDocument();
  });

  it('asks for no count until a file is chosen', async () => {
    const user = open({ hasCanvasSource: false });
    await batchTab(user);

    await waitFor(() => expect(api.listMetrics).toHaveBeenCalled());
    expect(api.previewBatchUpload).not.toHaveBeenCalled();
  });
});

describe('stepping from the dialog', () => {
  // Stepping used to be reachable only by editing the source node on the
  // canvas, even though it is the setting that decides how big the batch is.
  async function creditRiskTab() {
    const user = open({ hasCanvasSource: false });
    await batchTab(user);
    await user.selectOptions(await screen.findByLabelText(/data source/i), 'credit_risk');
    return user;
  }

  it('offers it alongside split and seed', async () => {
    await creditRiskTab();
    expect(await screen.findByLabelText(/walk the window/i)).toHaveValue('none');
  });

  it('re-counts the batch when it changes', async () => {
    const user = await creditRiskTab();
    api.previewBatchSource.mockResolvedValue({
      total: 18, samples: 3, steps: 6, steps_min: 6, dates: ['2024-01-01', '2024-06-30'],
    });
    await user.selectOptions(screen.getByLabelText(/walk the window/i), 'monthly');

    await waitFor(() => expect(api.previewBatchSource).toHaveBeenCalledWith(
      'g1', expect.objectContaining({ step: 'monthly' })));
    expect(await screen.findByText(/18 runs — 3 samples stepped over 6 dates each/))
      .toBeInTheDocument();
  });

  it('runs with the same stepping it counted', async () => {
    // A preview that counted 18 and a run that does 3 is worse than no count.
    const user = await creditRiskTab();
    api.runBatchSource.mockResolvedValue({ batch_id: 'b1' });
    await user.selectOptions(screen.getByLabelText(/walk the window/i), 'weekly');
    const run = await screen.findByRole('button', { name: /run batch/i });
    await waitFor(() => expect(run).toBeEnabled());     // the count must land first
    await user.click(run);

    await waitFor(() => expect(api.runBatchSource).toHaveBeenCalledWith(
      'g1', expect.objectContaining({ step: 'weekly' })));
  });

  it('starts the batch with the new graph id returned by save', async () => {
    const beforeRun = vi.fn().mockResolvedValue({ id: 'renamed-graph' });
    api.runBatchCanvas.mockResolvedValue({ batch_id: 'b1' });
    const user = open({ beforeRun });
    await batchTab(user);
    const run = await screen.findByRole('button', { name: /run batch/i });
    await waitFor(() => expect(run).toBeEnabled());
    await user.click(run);

    await waitFor(() => expect(api.runBatchCanvas).toHaveBeenCalledWith(
      'renamed-graph', expect.objectContaining({ workers: 2 })));
  });
});


describe('the single run is planned before it is offered', () => {
  // Phase 1 of RUN_INPUT_FLOW_PLAN.md: the form shows only what the plan says
  // and cannot be submitted until there is one.
  const withInputs = {
    ...PLAN,
    inputs: [{ name: 'topic', type: 'str', description: 't', required: true, consumed_by: 'a' }],
  };

  it('disables Run while planning and enables it once the plan arrives', async () => {
    let finish;
    api.runPlan.mockReturnValue(new Promise((r) => { finish = r; }));
    open({ hasCanvasSource: false });

    const run = await screen.findByRole('button', { name: /^run$/i });
    expect(run).toBeDisabled();

    finish(withInputs);
    await waitFor(() => expect(run).toBeEnabled());
  });

  it('offers only the start points the plan allows', async () => {
    api.runPlan.mockResolvedValue({ ...withInputs, nodes: [{ name: 'feed' }, { name: 'a' }],
      start_points: ['a'] });
    open({ hasCanvasSource: false });

    const select = await screen.findByLabelText(/start from/i);
    const names = [...select.querySelectorAll('option')].map((o) => o.value);
    expect(names).toEqual(['', 'a']);
  });

  it('drops a plan that arrives after a newer request was made', async () => {
    // Switching Start from twice: the first answer lands last and must not
    // overwrite the second.
    let first;
    api.runPlan
      .mockReturnValueOnce(Promise.resolve({ ...withInputs, start_points: ['a', 'b'] }))
      .mockReturnValueOnce(new Promise((r) => { first = r; }))      // for start_at b, slow
      .mockReturnValueOnce(Promise.resolve({ ...withInputs, start_at: ['a'],
        inputs: [{ name: 'fresh', type: 'str', description: '', required: true }] }));
    const user = open({ hasCanvasSource: false });
    const select = await screen.findByLabelText(/start from/i);
    await user.selectOptions(select, 'b');
    await user.selectOptions(select, 'a');
    await screen.findByLabelText(/fresh/, { selector: 'textarea' });

    first({ ...withInputs, start_at: ['b'],
      inputs: [{ name: 'stale', type: 'str', description: '', required: true }] });
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByText(/stale/)).not.toBeInTheDocument();
    expect(screen.getByLabelText(/fresh/, { selector: 'textarea' })).toBeInTheDocument();
  });

  it('submits the plan it showed', async () => {
    api.runPlan.mockResolvedValue(withInputs);
    const user = open({ hasCanvasSource: false });
    await user.type(await screen.findByLabelText(/topic/, { selector: 'textarea' }), 'rates');
    await user.click(screen.getByRole('button', { name: /^run$/i }));

    await waitFor(() => expect(user.submit).toHaveBeenCalledWith(
      { topic: 'rates' }, undefined, undefined, 'plan:aaaa', undefined));
  });

  it('stays open and says why when the launch is refused', async () => {
    api.runPlan.mockResolvedValue(withInputs);
    const onSubmit = vi.fn().mockResolvedValue({ ok: false, error: 'Missing required input' });
    const user = open({ hasCanvasSource: false, onSubmit });
    await user.type(await screen.findByLabelText(/topic/, { selector: 'textarea' }), 'rates');
    await user.click(screen.getByRole('button', { name: /^run$/i }));

    expect(await screen.findByText(/Missing required input/)).toBeInTheDocument();
    expect(screen.getByLabelText(/topic/, { selector: 'textarea' })).toHaveValue('rates');   // nothing lost
  });

  it('plans again when the server says the plan went stale', async () => {
    api.runPlan.mockResolvedValue(withInputs);
    const onSubmit = vi.fn().mockResolvedValue({ ok: false, stale: true, error: 'changed' });
    const user = open({ hasCanvasSource: false, onSubmit });
    await user.type(await screen.findByLabelText(/topic/, { selector: 'textarea' }), 'rates');
    await waitFor(() => expect(api.runPlan).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole('button', { name: /^run$/i }));

    await waitFor(() => expect(api.runPlan).toHaveBeenCalledTimes(2));
    expect(await screen.findByText(/changed/)).toBeInTheDocument();
  });

  it('shows the plan warnings, such as a source that yields several records', async () => {
    api.runPlan.mockResolvedValue({ ...PLAN,
      source: { node: 'feed', cardinality: 4, single_policy: 'first' },
      warnings: ["Source 'feed' yields 4 records; a single run takes the first one."] });
    open();

    expect(await screen.findByText(/yields 4 records/)).toBeInTheDocument();
  });
});


describe('a source that yields several records forces a choice', () => {
  // Phase 2: "single run" on a four-record source used to mean the first
  // record, silently. Now nothing runs until you say which.
  const fourRecords = {
    ...PLAN,
    source: { node: 'feed', type: 'credit_risk', cardinality: 4, single_policy: 'first' },
    warnings: [],
  };

  it('does not let Run through until a record is chosen', async () => {
    api.runPlan.mockResolvedValue(fourRecords);
    open();
    await screen.findByText(/yields 4 records/i);
    expect(screen.getByRole('button', { name: /^run$/i })).toBeDisabled();
  });

  it('runs the first record when told to', async () => {
    api.runPlan.mockResolvedValue(fourRecords);
    const user = open();
    await user.click(await screen.findByLabelText(/run the first record/i));
    await user.click(screen.getByRole('button', { name: /^run$/i }));

    await waitFor(() => expect(user.submit).toHaveBeenCalledWith(
      {}, undefined, undefined, 'plan:aaaa', 0));
  });

  it('lists the records to choose from, and runs the chosen one', async () => {
    api.runPlan.mockResolvedValue(fourRecords);
    const user = open();
    await screen.findByText(/yields 4 records/i);
    api.runPlan.mockResolvedValue({ ...fourRecords, records: [
      { index: 0, summary: { company: 'Moderna', as_of: '2026-03-25' } },
      { index: 1, summary: { company: 'Wayfair', as_of: '2026-06-02' } },
    ] });
    await user.click(screen.getByLabelText(/choose a record/i));

    await waitFor(() => expect(api.runPlan).toHaveBeenLastCalledWith(
      'g1', expect.objectContaining({ includeRecords: true })));
    await user.selectOptions(await screen.findByLabelText(/^record$/i), '1');
    await user.click(screen.getByRole('button', { name: /^run$/i }));

    await waitFor(() => expect(user.submit).toHaveBeenCalledWith(
      {}, undefined, undefined, 'plan:aaaa', 1));
  });

  it('offers to run them all as a batch instead', async () => {
    api.runPlan.mockResolvedValue(fourRecords);
    const user = open();
    await user.click(await screen.findByRole('button', { name: /run all 4 as a batch/i }));

    expect(await screen.findByLabelText(/data source/i)).toBeInTheDocument();
  });
});

describe('start from says what it changes', () => {
  it('names what runs, what is skipped, and what you must supply', async () => {
    api.runPlan.mockResolvedValue({ ...PLAN,
      start_at: ['b'], nodes: [{ name: 'b' }, { name: 'c' }], skipped: ['a'],
      inputs: [{ name: 'x', type: 'str', description: '', required: true, consumed_by: 'b' }],
      prefill: {}, prefill_from_run: null, prefill_missing: ['x'],
      start_points: ['a', 'b'] });
    const user = open({ hasCanvasSource: false });
    await user.selectOptions(await screen.findByLabelText(/start from/i), 'b');

    const said = (await screen.findByTestId('start-from-summary')).textContent;
    expect(said).toMatch(/Runs:.*b.*c/);
    expect(said).toMatch(/Skipped:.*a/);
    expect(said).toMatch(/Needs your input:.*x/);
  });
});

describe('typed inputs', () => {
  it('lets an optional boolean stay unset', async () => {
    api.runPlan.mockResolvedValue({ ...PLAN, inputs: [
      { name: 'verbose', type: 'bool', description: '', required: false, consumed_by: 'a' }] });
    const user = open({ hasCanvasSource: false });
    const field = await screen.findByLabelText(/verbose/);
    expect(field).toHaveValue('');                       // unset, not false
    await user.click(screen.getByRole('button', { name: /^run$/i }));

    await waitFor(() => expect(user.submit).toHaveBeenCalled());
    expect(user.submit.mock.calls[0][0]).toEqual({});     // nothing sent
  });

  it('sends false when false is chosen', async () => {
    api.runPlan.mockResolvedValue({ ...PLAN, inputs: [
      { name: 'verbose', type: 'bool', description: '', required: false, consumed_by: 'a' }] });
    const user = open({ hasCanvasSource: false });
    await user.selectOptions(await screen.findByLabelText(/verbose/), 'false');
    await user.click(screen.getByRole('button', { name: /^run$/i }));

    await waitFor(() => expect(user.submit.mock.calls[0][0]).toEqual({ verbose: false }));
  });

  it('refuses JSON null for an object', () => {
    expect(parseValue('dict', 'null').error).toMatch(/object/);
    expect(parseValue('object', 'null').error).toMatch(/object/);
  });

  it('leaves an empty optional string out rather than sending ""', async () => {
    api.runPlan.mockResolvedValue({ ...PLAN, inputs: [
      { name: 'note', type: 'str', description: '', required: false, consumed_by: 'a' }] });
    const user = open({ hasCanvasSource: false });
    await screen.findByLabelText(/note/, { selector: 'textarea' });
    await user.click(screen.getByRole('button', { name: /^run$/i }));

    await waitFor(() => expect(user.submit).toHaveBeenCalled());
    expect(user.submit.mock.calls[0][0]).toEqual({});
  });

  it('remembers last inputs per workflow revision and start point', async () => {
    api.runPlan.mockResolvedValue({ ...PLAN, inputs: [
      { name: 'topic', type: 'str', description: '', required: true, consumed_by: 'a' }] });
    const user = open({ hasCanvasSource: false });
    await user.type(await screen.findByLabelText(/topic/, { selector: 'textarea' }), 'rates');
    await user.click(screen.getByRole('button', { name: /^run$/i }));
    await waitFor(() => expect(user.submit).toHaveBeenCalled());

    const keys = Object.keys(window.localStorage);
    expect(keys.some((k) => k.includes('g1') && k.includes('rev:1'))).toBe(true);
  });
});

describe('the batch preview is authoritative', () => {
  it('keeps Run batch disabled while the count is still coming', async () => {
    api.previewBatchCanvas.mockReturnValue(new Promise(() => {}));
    const user = open();
    await batchTab(user);
    expect(await screen.findByRole('button', { name: /run batch/i })).toBeDisabled();
  });

  it('keeps Run batch disabled when the preview failed', async () => {
    api.previewBatchCanvas.mockRejectedValue({ body: { detail: 'no such split' } });
    const user = open();
    await batchTab(user);
    await screen.findByText(/no such split/);
    expect(screen.getByRole('button', { name: /run batch/i })).toBeDisabled();
  });

  it('says how many node executions the batch is', async () => {
    api.previewBatchCanvas.mockResolvedValue({ ...STEPPED, node_count: 7, node_executions: 168 });
    const user = open();
    await batchTab(user);
    expect(await screen.findByText(/24 records × 7 nodes = up to 168 node executions/))
      .toBeInTheDocument();
  });

  it('previews with the scoring it will run with', async () => {
    api.listMetrics.mockResolvedValue({ metrics: [{ name: 'exact_match', description: '' }] });
    const user = open();
    await batchTab(user);
    await user.selectOptions(await screen.findByLabelText(/score the results/i), 'exact_match');
    await user.type(screen.getByLabelText(/field holding the expected answer/i), 'verdict');

    await waitFor(() => expect(api.previewBatchCanvas).toHaveBeenLastCalledWith(
      'g1', expect.objectContaining({ metric: 'exact_match', label_key: 'verdict' })));
  });

  it('tells you that stopping only spares what has not started', async () => {
    const user = open();
    await batchTab(user);
    expect(await screen.findByText(/already running (will|cannot)/i)).toBeInTheDocument();
  });
});


describe('workers are not capped at five', () => {
  it('offers one worker per sample and accepts it', async () => {
    api.previewBatchSource.mockResolvedValue({ total: 96, samples: 16, steps: 6, steps_min: 6, dates: ['a', 'b'], workers: 2, node_count: 8, node_executions: 768, fields: ['sample_json'] });
    const user = open();
    await batchTab(user);
    await user.selectOptions(await screen.findByLabelText(/data source/i), 'credit_risk');
    await user.click(await screen.findByRole('button', { name: /one per sample \(16\)/i }));
    expect(screen.getByLabelText(/workers/i)).toHaveValue(16);
  });
});

describe('run confirmation summary', () => {
  it('uses the plan source and shows missing inputs before submission', async () => {
    api.runPlan.mockResolvedValue({ ...PLAN, skipped: ['draft'], inputs: [
      { name: 'topic', type: 'str', required: true },
    ] });
    const user = open({ hasCanvasSource: true });
    const summary = await screen.findByRole('region', { name: 'Run summary' });
    expect(summary).toHaveTextContent('Participating nodes: a, b');
    expect(summary).toHaveTextContent('Excluded nodes: draft');
    expect(summary).toHaveTextContent('Data: Inputs below');
    expect(summary).toHaveTextContent('Missing inputs: topic');
    await user.type(screen.getByLabelText(/topic/, { selector: 'textarea' }), 'rates');
    expect(summary).toHaveTextContent('Required inputs are ready.');
  });

  it('keeps the start selector after narrowing to the last node', async () => {
    api.runPlan.mockResolvedValueOnce(PLAN).mockResolvedValueOnce({
      ...PLAN, start_at: ['b'], nodes: [{ name: 'b' }], skipped: ['a'],
    }).mockResolvedValue(PLAN);
    const user = open();
    await user.selectOptions(await screen.findByLabelText(/start from/i), 'b');
    await waitFor(() => expect(screen.getByRole('region', { name: 'Run summary' }))
      .toHaveTextContent('Participating nodes: b'));
    await user.selectOptions(screen.getByLabelText(/start from/i), '');
    await waitFor(() => expect(screen.getByRole('region', { name: 'Run summary' }))
      .toHaveTextContent('Participating nodes: a, b'));
  });

  it('requires an explicit selection when the source count is unknown', async () => {
    api.runPlan.mockResolvedValue({ ...PLAN, source: { node: 'feed', cardinality: null } });
    const user = open();
    await screen.findByText(/unknown number of/);
    expect(screen.getByRole('button', { name: /^Run$/ })).toBeDisabled();
    await user.click(screen.getByLabelText(/run the first record/i));
    expect(screen.getByRole('region', { name: 'Run summary' })).toHaveTextContent('record #1 only');
    await user.click(screen.getByRole('button', { name: /^Run$/ }));
    expect(user.submit).toHaveBeenCalledWith({}, undefined, undefined, PLAN.plan_id, 0);
  });

  it('refreshes a changed plan instead of accepting its record list', async () => {
    const sourcePlan = { ...PLAN, source: { node: 'feed', cardinality: 4 } };
    api.runPlan.mockResolvedValueOnce(sourcePlan)
      .mockResolvedValueOnce({ ...sourcePlan, plan_id: 'plan:new', records: [{ index: 0, summary: { company: 'old' } }] })
      .mockResolvedValue({ ...sourcePlan, plan_id: 'plan:new' });
    const user = open();
    await user.click(await screen.findByLabelText(/choose a record/i));
    await screen.findByText(/workflow changed/);
    await waitFor(() => expect(api.runPlan).toHaveBeenCalledTimes(3));
    expect(screen.getByRole('button', { name: /^Run$/ })).toBeDisabled();
    expect(user.submit).not.toHaveBeenCalled();
  });
});

it('uses the locally saved input instead of replacing it with older run history', async () => {
  localStorage.setItem('evoagentx-studio:last-inputs:g1:rev:1:', JSON.stringify({material:'new material'}));
  api.runPlan.mockResolvedValue({...PLAN,inputs:[{name:'material',type:'str',required:true}],prefill:{material:'old material'}});
  open();
  expect(await screen.findByDisplayValue('new material')).toBeInTheDocument();
  expect(screen.queryByDisplayValue('old material')).not.toBeInTheDocument();
});

it('rejects numeric values that cannot be transmitted accurately', () => {
  expect(parseValue('float','Infinity').error).toBeTruthy();
  expect(parseValue('float','1e999').error).toBeTruthy();
  expect(parseValue('int','9007199254740993').error).toBeTruthy();
});


it('loads a material file and submits its exact contents', async () => {
  api.runPlan.mockResolvedValue({ ...PLAN, inputs: [{ name: 'material', type: 'str', required: true }] });
  const user = open();
  const field = await screen.findByRole('textbox', { name: /material/ });
  const content = '# Report\nRevenue decreased.\n';
  const file = new File([content], 'report.md', { type: 'text/markdown' });
  file.arrayBuffer = async () => new TextEncoder().encode(content).buffer;
  await user.upload(screen.getByLabelText('Load file into material'), file);
  await waitFor(() => expect(field).toHaveValue(content));
  await user.click(screen.getByRole('button', { name: 'Run', exact: true }));
  expect(user.submit).toHaveBeenCalledWith({ material: content }, undefined, undefined, PLAN.plan_id, undefined);
});

it('keeps the original material after a failed file read and allows correction', async () => {
  api.runPlan.mockResolvedValue({ ...PLAN, inputs: [{ name: 'material', type: 'str', required: true }] });
  const user = open();
  const field = await screen.findByRole('textbox', { name: /material/ });
  await user.type(field, 'original');
  const file = new File([''], 'empty.txt', { type: 'text/plain' });
  file.arrayBuffer = async () => new ArrayBuffer(0);
  await user.upload(screen.getByLabelText('Load file into material'), file);
  await screen.findByRole('alert');
  expect(field).toHaveValue('original');
  expect(screen.getByRole('button', { name: 'Run', exact: true })).toBeDisabled();
  await user.type(field, ' corrected');
  expect(screen.getByRole('button', { name: 'Run', exact: true })).toBeEnabled();
});


describe('collect API inputs before batch execution', () => {
  it('waits for collection and runs the exact saved input snapshot', async () => {
    api.previewBatchCanvas.mockImplementation(async (_id, body) => body.collection_id
      ? { total: 2, fields: ['news_batch'] }
      : { requires_collection: true, source: { type: 'gdelt_news', query: 'Acme' } });
    api.collectSource.mockResolvedValue({ id: 'collection-1', status: 'collecting', total: 2, completed: 0 });
    api.sourceCollection.mockResolvedValue({ id: 'collection-1', status: 'ready', record_count: 2, sample: [{ news_batch: 'saved news' }] });
    api.runBatchCanvas.mockResolvedValue({ batch_id: 'b' });
    const user = open();
    await batchTab(user);
    await user.click(await screen.findByRole('button', { name: 'Collect data' }));
    expect(screen.queryByRole('button', { name: 'Run batch' })).not.toBeInTheDocument();
    expect(api.runBatchCanvas).not.toHaveBeenCalled();
    await screen.findByText(/Collection complete/, {}, { timeout: 2500 });
    const run = await screen.findByRole('button', { name: 'Run batch (2)' });
    await waitFor(() => expect(run).toBeEnabled());
    await user.click(run);
    expect(api.runBatchCanvas).toHaveBeenCalledWith('g1', expect.objectContaining({ collection_id: 'collection-1' }));
    expect(api.collectSource).toHaveBeenCalledTimes(1);
  });

  it('can stop collection without launching the workflow', async () => {
    api.previewBatchCanvas.mockResolvedValue({ requires_collection: true, source: { type: 'gdelt_news', query: 'Acme' } });
    api.collectSource.mockResolvedValue({ id: 'collection-2', status: 'collecting' });
    api.stopSourceCollection.mockResolvedValue({ status: 'stopping' });
    api.sourceCollection.mockResolvedValue({ id: 'collection-2', status: 'cancelled', error: 'Collection stopped.' });
    const user = open();
    await batchTab(user);
    await user.click(await screen.findByRole('button', { name: 'Collect data' }));
    await user.click(await screen.findByRole('button', { name: 'Stop collection' }));
    expect(api.stopSourceCollection).toHaveBeenCalledWith('g1', 'collection-2');
    await screen.findByText('Collection stopped.', {}, { timeout: 2500 });
    expect(screen.queryByRole('button', { name: 'Run batch' })).not.toBeInTheDocument();
    expect(api.runBatchCanvas).not.toHaveBeenCalled();
  });
});

it('offers streaming batches with a record threshold and no duplicate final run', async () => {
  api.previewBatchCanvas.mockResolvedValue({ requires_collection: true, source: { type: 'gdelt_news', query: 'Acme' } });
  api.collectSource.mockResolvedValue({ id: 'stream-1', mode: 'stream', status: 'collecting', record_count: 0, batches: [] });
  api.sourceCollection.mockResolvedValue({ id: 'stream-1', mode: 'stream', status: 'completed', batch_size: 3, record_count: 7, submitted_records: 7, batches: [{ id: 'b1', total: 3, status: 'succeeded' }, { id: 'b2', total: 3, status: 'succeeded' }, { id: 'b3', total: 1, status: 'succeeded' }] });
  const user = open();
  await batchTab(user);
  await user.selectOptions(await screen.findByLabelText('Execution mode'), 'stream');
  await user.clear(screen.getByLabelText('Records per batch'));
  await user.type(screen.getByLabelText('Records per batch'), '3');
  await user.click(screen.getByRole('button', { name: 'Start collecting and running' }));
  expect(api.collectSource).toHaveBeenCalledWith('g1', expect.objectContaining({ mode: 'stream', batch_size: 3, workers: 2 }));
  await screen.findByText(/Collection and all batches completed/, {}, { timeout: 2500 });
  expect(screen.getByRole('button', { name: 'View batch 3' })).toBeEnabled();
  expect(screen.queryByRole('button', { name: 'Run batch' })).not.toBeInTheDocument();
  expect(api.runBatchCanvas).not.toHaveBeenCalled();
});

it('uses the selected release in both batch preview and batch start', async () => {
 const version='2026-09-10-random-dev-test-v1';
 api.creditRiskSource.mockResolvedValue({datasets:[{id:'contemporary',label:'Legacy'},{id:version,label:'Random 60:40'}],splits:{dev:64,test:43},fields:['company']});
 api.previewBatchSource.mockResolvedValue({total:707});
 api.runBatchSource.mockResolvedValue({batch_id:'selected-batch'});
 const user=open({hasCanvasSource:false});
 await batchTab(user);
 await user.selectOptions(screen.getByLabelText('Data source'),'credit_risk');
 await screen.findByRole('option',{name:'Random 60:40'});
 await user.selectOptions(screen.getByLabelText('Dataset version'),version);
 await user.selectOptions(screen.getByLabelText('Split'),'test');
 expect(screen.getByLabelText('Walk the window')).toBeEnabled();
 await user.selectOptions(screen.getByLabelText('Walk the window'),'weekly');
 await waitFor(()=>expect(api.previewBatchSource).toHaveBeenLastCalledWith('g1',expect.objectContaining({dataset:version,split:'test',step:'weekly'})));
 await waitFor(()=>expect(screen.getByRole('button',{name:'Run batch (707)'})).toBeEnabled());
 await user.click(screen.getByRole('button',{name:'Run batch (707)'}));
 expect(api.runBatchSource).toHaveBeenCalledWith('g1',expect.objectContaining({dataset:version,split:'test',step:'weekly'}));
});

it('offers whole-dataset preprocessing for a saved local input and submits it before batches', async () => {
  api.listCustomTools.mockResolvedValue({tools:[{name:'prepare_all', params:[{name:'records'}]}]});
  api.collectSource.mockResolvedValue({id:'prepare-job', status:'completed', mode:'prepare', record_count:4, processed_count:3, batches:[]});
  open();
  await userEvent.click(screen.getByRole('button', {name:'Batch run'}));
  await userEvent.selectOptions(screen.getByLabelText('Execution mode'), 'prepare');
  const start = screen.getByRole('button', {name:'Preprocess all and run batches'});
  expect(start).toBeDisabled();
  await userEvent.selectOptions(screen.getByLabelText(/Whole-dataset preprocessor/), 'prepare_all');
  await userEvent.click(start);
  await waitFor(() => expect(api.collectSource).toHaveBeenCalledWith('g1', expect.objectContaining({mode:'prepare', preprocess_tool:'prepare_all', batch_size:10})));
  expect(api.runBatchCanvas).not.toHaveBeenCalled();
  expect(await screen.findByText(/4 input records.*3 processed records/)).toBeInTheDocument();
});

it('sends native API batch size instead of workers to preview and run', async () => {
  api.runBatchCanvas.mockResolvedValue({batch_id:'native'});
  const user = open(); await batchTab(user);
  await user.selectOptions(screen.getByLabelText('Model execution'), 'native');
  await user.clear(screen.getByLabelText('API batch size'));
  await user.type(screen.getByLabelText('API batch size'), '16');
  expect(screen.queryByLabelText('Workers (records run at the same time)')).not.toBeInTheDocument();
  await waitFor(() => expect(api.previewBatchCanvas).toHaveBeenLastCalledWith('g1', {llm_batch_size:16}));
  const run = await screen.findByRole('button', {name:'Run batch (24)'});
  await waitFor(() => expect(run).toBeEnabled());
  await user.click(run);
  expect(api.runBatchCanvas).toHaveBeenCalledWith('g1', {llm_batch_size:16});
});

it('uses the Input batch size for native requests without a second size field', async () => {
  api.previewBatchCanvas.mockResolvedValue({...STEPPED, source:{config:{type:'dataloader',read_batch_size:7}}});
  api.runBatchCanvas.mockResolvedValue({batch_id:'shared'});
  const user=open();await batchTab(user);
  await screen.findByText(/Batch size: 7/);
  await user.selectOptions(screen.getByLabelText('Model execution'),'native');
  expect(screen.queryByLabelText('API batch size')).not.toBeInTheDocument();
  await waitFor(()=>expect(api.previewBatchCanvas).toHaveBeenLastCalledWith('g1',{llm_batch_size:7}));
  const button=screen.getByRole('button',{name:'Run batch (24)'});
  await waitFor(()=>expect(button).toBeEnabled());
  await user.click(button);
  expect(api.runBatchCanvas).toHaveBeenCalledWith('g1',{llm_batch_size:7});
});
