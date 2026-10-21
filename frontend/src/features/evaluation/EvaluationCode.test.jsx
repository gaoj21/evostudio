import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { beforeEach, expect, it, vi } from 'vitest';

vi.mock('../../api.js', () => ({ api: {
  evaluatorDraft: vi.fn(), saveEvaluatorDraft: vi.fn(), deleteEvaluatorDraft: vi.fn(),
  inspectEvaluatorCode: vi.fn(), listDataResources: vi.fn(),
} }));
const { api } = await import('../../api.js');
import EvaluationCode from './EvaluationCode.jsx';

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  api.evaluatorDraft.mockResolvedValue({ code: 'def evaluate(records):\n    return {}\n', config: {},
                                         saved_at: '2026-01-01T00:00:00Z' });
  api.saveEvaluatorDraft.mockResolvedValue({});
  api.deleteEvaluatorDraft.mockResolvedValue({ cleared: true });
  api.inspectEvaluatorCode.mockResolvedValue({ params: [] });
  api.listDataResources.mockResolvedValue({ resources: [] });
});

it('Discard draft deletes the server draft instead of saving an empty one', async () => {
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  const user = userEvent.setup();
  render(<EvaluationCode graphId="g1" onChange={vi.fn()} />);
  await user.click(await screen.findByRole('button', { name: 'Discard draft' }));
  await waitFor(() => expect(api.deleteEvaluatorDraft).toHaveBeenCalledWith('g1'));
  expect(api.saveEvaluatorDraft).not.toHaveBeenCalledWith('g1', { code: '', config: {} });
});
