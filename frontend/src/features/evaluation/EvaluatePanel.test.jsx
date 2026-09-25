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

describe('the workflow has one evaluation: this code', () => {
  beforeEach(() => {
    api.saveGraphEvaluators.mockImplementation(async (id, list) => ({ evaluators: list }));
  });

  it('keeps confirmed code as the workflow evaluation, with no name to give', async () => {
    const onChanged = vi.fn();
    const { container } = render(<EvaluatePanel graphId="g1" onEvaluatorsChanged={onChanged} />);
    const view = within(container);
    const user = userEvent.setup();
    await withCode(view, user);
    await waitFor(() => expect(api.saveGraphEvaluators).toHaveBeenCalledWith('g1', [expect.objectContaining({
      name: 'evaluation', code: CODE, timing: 'manual', timeout: 120, enabled: true })]));
    expect(onChanged).toHaveBeenCalled();
    expect(view.queryByLabelText('Name')).toBeNull();
    expect(view.queryByRole('button', { name: 'Save as evaluator' })).toBeNull();
    expect(view.queryByText(/new evaluator/i)).toBeNull();
  });

  it('opens with the kept code and its objective, and keeps the metric picked from a report', async () => {
    api.graphEvaluators.mockResolvedValue({ evaluators: [
      { name: 'evaluation', code: CODE, config: { field: 'answer' }, metric: '', direction: 'maximize', timing: 'manual', enabled: true }] });
    api.previewGraphEvaluator.mockResolvedValue({ status: 'success', metrics: { score: 0.8, recall: 0.5 } });
    const { view, user } = open();
    await waitFor(() => expect(view.getByLabelText('Python Evaluator code')).toHaveValue(CODE));
    await waitFor(() => expect(view.getByLabelText('Saved batch or run')).toHaveValue('batch:b1'));
    await user.click(view.getByRole('button', { name: 'Preview' }));
    await waitFor(() => expect(view.getByLabelText('Metric')).toHaveValue('score'));
    await waitFor(() => expect(api.saveGraphEvaluators).toHaveBeenCalledWith('g1', [expect.objectContaining({
      name: 'evaluation', metric: 'score', config: { field: 'answer' } })]));
  });

  it('leaves anything else saved on the workflow alone', async () => {
    const other = { name: 'legacy', code: 'x', metric: 'm', enabled: true };
    api.graphEvaluators.mockResolvedValue({ evaluators: [
      { name: 'evaluation', code: CODE, config: {}, metric: 'score', direction: 'maximize', timeout: 120, enabled: true }, other] });
    const { view, user } = open();
    await waitFor(() => expect(view.getByLabelText('Python Evaluator code')).toHaveValue(CODE));
    await withCode(view, user, CODE + '# changed\n');
    await waitFor(() => expect(api.saveGraphEvaluators).toHaveBeenCalledWith('g1',
      [other, expect.objectContaining({ name: 'evaluation', code: CODE + '# changed\n' })]));
  });
});
