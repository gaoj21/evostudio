import { render, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../api.js', () => ({ api: {
  inspectEvaluatorCode: vi.fn(), previewGraphEvaluator: vi.fn(), runGraphEvaluator: vi.fn(),
  graphEvaluators: vi.fn(), saveGraphEvaluators: vi.fn(), deleteGraphEvaluator: vi.fn(),
  evaluatorDraft: vi.fn(), saveEvaluatorDraft: vi.fn(),
  listBatches: vi.fn(), listRuns: vi.fn(), listDataResources: vi.fn(),
} }));
const { api } = await import('../../api.js');
import EvaluatePanel from './EvaluatePanel.jsx';

const CODE = 'def evaluate(records):\n    return {"metrics": {"score": 1}}\n';
const iface = { kind: 'function', params: [
  { name: 'threshold', type: 'float', required: false, default: 0.5, description: 'Minimum score' },
  { name: 'field', type: 'str', required: true },
  { name: 'strict', type: 'bool', required: false, default: false },
] };

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  api.evaluatorDraft.mockResolvedValue({ code: '', config: {} });
  api.saveEvaluatorDraft.mockResolvedValue({ saved_at: '2026-02-01T10:00:00Z' });
  api.graphEvaluators.mockResolvedValue({ evaluators: [] });
  api.listBatches.mockResolvedValue([{ batch_id: 'b1', status: 'succeeded', total: 12 }]);
  api.listRuns.mockResolvedValue([{ run_id: 'r1', status: 'success' }]);
  api.listDataResources.mockResolvedValue({ resources: [] });
  api.inspectEvaluatorCode.mockResolvedValue(iface);
});

const open = () => {
  const { container, unmount } = render(<EvaluatePanel graphId="g1" />);
  return { view: within(container), user: userEvent.setup(), container, unmount };
};

async function withCode(view, user, code = CODE) {
  const editor = await view.findByLabelText('Python Evaluator code');
  await user.clear(editor);
  await user.paste(code);
  await user.click(view.getByRole('button', { name: 'Confirm code' }));
}

describe('the code declares the form', () => {
  it('renders one typed field per declared parameter, with defaults and required marks', async () => {
    const { view, user } = open();
    await withCode(view, user);
    await user.click(view.getByRole('button', { name: 'Check code' }));
    await waitFor(() => expect(api.inspectEvaluatorCode).toHaveBeenCalledWith(CODE));
    expect(await view.findByTestId('interface-kind')).toHaveTextContent('Evaluation function');
    expect(view.getByLabelText(/threshold/)).toHaveValue(0.5);
    expect(view.getByLabelText(/field \*/)).toBeRequired();
    expect(view.getByLabelText(/strict/).tagName).toBe('SELECT');       // bool is a choice, not free text
  });

  it('shows the error with the line instead of a form', async () => {
    api.inspectEvaluatorCode.mockResolvedValue({ error: 'SyntaxError: line 3: invalid syntax', params: [] });
    const { view, user } = open();
    await withCode(view, user);
    await user.click(view.getByRole('button', { name: 'Check code' }));
    expect(await view.findByTestId('interface-error')).toHaveTextContent('line 3');
    expect(view.queryByTestId('interface-kind')).not.toBeInTheDocument();
  });
});

describe('preview and run', () => {
  const report = { report: { status: 'success', metrics: { score: 0.5, recall: 0.25 }, objective: { metric: 'score' } } };

  it('previews the code against a saved batch without saving anything', async () => {
    api.previewGraphEvaluator.mockResolvedValue(report);
    const { view, user } = open();
    await withCode(view, user);
    await user.click(view.getByRole('button', { name: 'Check code' }));
    await view.findByLabelText(/threshold/);
    await waitFor(() => expect(view.getByLabelText('Saved batch or run')).toHaveValue('batch:b1'));
    await user.click(view.getByRole('button', { name: 'Preview' }));
    await waitFor(() => expect(api.previewGraphEvaluator).toHaveBeenCalledWith('g1',
      expect.objectContaining({ code: CODE, batch_id: 'b1', config: expect.objectContaining({ threshold: 0.5 }) })));
    expect(api.runGraphEvaluator).not.toHaveBeenCalled();
    // The metric picker is filled from what the code actually returned.
    const metrics = [...(await view.findByLabelText('Metric')).querySelectorAll('option')].map((o) => o.value);
    expect(metrics).toEqual(['score', 'recall']);
    expect(view.getByText(/success · score 0.5/)).toBeInTheDocument();
  });

  it('runs against a saved run and keeps the report', async () => {
    api.runGraphEvaluator.mockResolvedValue(report);
    const { view, user } = open();
    await withCode(view, user);
    await waitFor(() => expect(view.getByLabelText('Saved batch or run')).toHaveValue('batch:b1'));
    await user.selectOptions(view.getByLabelText('Saved batch or run'), 'run:r1');
    await user.click(view.getByRole('button', { name: 'Run and save report' }));
    await waitFor(() => expect(api.runGraphEvaluator).toHaveBeenCalledWith('g1',
      expect.objectContaining({ code: CODE, run_id: 'r1' })));
  });
});

describe('the pasted code is kept as a draft', () => {
  it('restores the server draft when the panel opens', async () => {
    api.evaluatorDraft.mockResolvedValue({ code: 'pasted earlier', config: { field: 'answer' }, saved_at: '2026-02-01T10:00:00Z' });
    const { view } = open();
    expect(await view.findByDisplayValue('pasted earlier')).toBeInTheDocument();
    expect(view.getByTestId('draft-status')).toHaveTextContent('Draft saved');
  });

  it('autosaves what was typed and mirrors it in the browser', async () => {
    const { view, user } = open();
    const editor = await view.findByLabelText('Python Evaluator code');
    await user.click(editor);
    await user.paste('half written');
    await waitFor(() => expect(api.saveEvaluatorDraft).toHaveBeenCalledWith('g1', { code: 'half written', config: {} }));
    expect(view.getByTestId('draft-status')).toHaveTextContent('Draft saved');
    expect(window.localStorage.getItem('evoagentx-studio:evaluator-draft:g1')).toContain('half written');
  });

  it('keeps the browser copy and says so when the server refuses, and reopens with it', async () => {
    api.saveEvaluatorDraft.mockRejectedValue(new Error('offline'));
    const { view, user, unmount } = open();
    await user.click(await view.findByLabelText('Python Evaluator code'));
    await user.paste('only local');
    await waitFor(() => expect(view.getByTestId('draft-status')).toHaveTextContent('kept in this browser only'));
    unmount();
    const again = open();
    expect(await again.view.findByDisplayValue('only local')).toBeInTheDocument();
  });

  it('flushes the pending draft when the panel is left', async () => {
    const { view, user, unmount } = open();
    await view.findByLabelText('Python Evaluator code');
    api.saveEvaluatorDraft.mockClear();
    await user.click(view.getByLabelText('Python Evaluator code'));
    await user.paste('unflushed');
    unmount();
    await waitFor(() => expect(api.saveEvaluatorDraft).toHaveBeenCalledWith('g1', { code: 'unflushed', config: {} }));
  });

  it('discards the draft on both copies', async () => {
    api.evaluatorDraft.mockResolvedValue({ code: 'throw me away', config: {}, saved_at: '2026-02-01T10:00:00Z' });
    const { view, user } = open();
    await view.findByDisplayValue('throw me away');
    await user.click(view.getByRole('button', { name: 'Discard draft' }));
    await waitFor(() => expect(api.saveEvaluatorDraft).toHaveBeenCalledWith('g1', { code: '', config: {} }));
    expect(view.getByLabelText('Python Evaluator code')).toHaveValue('');
    expect(window.localStorage.getItem('evoagentx-studio:evaluator-draft:g1')).toBeNull();
  });
});

describe('the evaluators saved on the workflow', () => {
  beforeEach(() => {
    api.graphEvaluators.mockResolvedValue({ evaluators: [
      { name: 'quality', code: CODE, config: { field: 'answer' }, metric: 'score', timing: 'batch', enabled: true }] });
    api.saveGraphEvaluators.mockImplementation(async (id, list) => ({ evaluators: list }));
  });

  it('saves the written code under a name, with timing and time limit', async () => {
    api.graphEvaluators.mockResolvedValue({ evaluators: [] });
    const onChanged = vi.fn();
    const { container } = render(<EvaluatePanel graphId="g1" onEvaluatorsChanged={onChanged} />);
    const view = within(container);
    const user = userEvent.setup();
    await withCode(view, user);
    await user.type(view.getByLabelText('Name'), 'quality');
    await user.selectOptions(view.getByLabelText('Run evaluation'), 'batch');
    await user.click(view.getByRole('button', { name: 'Save as evaluator' }));
    await waitFor(() => expect(api.saveGraphEvaluators).toHaveBeenCalledWith('g1', [expect.objectContaining({
      name: 'quality', code: CODE, timing: 'batch', timeout: 120, enabled: true })]));
    expect(onChanged).toHaveBeenCalled();
    expect(await view.findByText('quality')).toBeInTheDocument();
  });

  it('lists, enables, edits and deletes them', async () => {
    api.deleteGraphEvaluator.mockResolvedValue({ deleted: 'quality' });
    const { view, user } = open();
    expect(await view.findByText('quality')).toBeInTheDocument();
    await user.click(view.getByLabelText('Enable quality'));
    await waitFor(() => expect(api.saveGraphEvaluators).toHaveBeenCalledWith('g1', [expect.objectContaining({ enabled: false })]));
    await user.click(view.getByRole('button', { name: 'Edit' }));
    expect(view.getByLabelText('Name')).toHaveValue('quality');
    expect(view.getByLabelText('Python Evaluator code')).toHaveValue(CODE);
    await user.click(view.getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(api.deleteGraphEvaluator).toHaveBeenCalledWith('g1', 'quality'));
    expect(await view.findByTestId('no-evaluators')).toBeInTheDocument();
  });

  it('runs a saved one by name on the chosen results', async () => {
    api.runGraphEvaluator.mockResolvedValue({ status: 'success', metrics: { score: 1 }, objective: { metric: 'score' } });
    const { view, user } = open();
    await view.findByText('quality');
    await waitFor(() => expect(view.getByLabelText('Saved batch or run')).toHaveValue('batch:b1'));
    await user.click(view.getByRole('button', { name: 'Run' }));
    await waitFor(() => expect(api.runGraphEvaluator).toHaveBeenCalledWith('g1', { name: 'quality', batch_id: 'b1' }));
    expect(await view.findByText(/quality · success · score 1/)).toBeInTheDocument();
  });
});
