import { expect, it, vi } from 'vitest';
import { clearCodeDrafts, codeDraftKeys, getCodeDraft, hasCodeDrafts, keepCodeDraft, moveCodeDraft, onCodeDraftsChange, setCodeDraftOpen } from './codeDrafts.js';

it('reports whether any editor holds unconfirmed code', () => {
  const seen = vi.fn();
  const stop = onCodeDraftsChange(seen);
  const a = {}, b = {};
  setCodeDraftOpen(a, true); setCodeDraftOpen(b, true);
  setCodeDraftOpen(a, false);
  expect(hasCodeDrafts()).toBe(true);
  setCodeDraftOpen(b, false);
  expect(hasCodeDrafts()).toBe(false);
  expect(seen.mock.calls.map(c => c[0])).toEqual([true, true, true, false]);
  stop();
});

it('keeps a keyed draft until it matches its confirmed code, moves it, and clears on request', () => {
  keepCodeDraft('dataset:a', 'x = 1', '');
  expect(getCodeDraft('dataset:a')).toEqual({ text: 'x = 1', base: '' });
  moveCodeDraft('dataset:a', 'dataset:b');
  expect(getCodeDraft('dataset:a')).toBe(null);
  expect(getCodeDraft('dataset:b').text).toBe('x = 1');
  keepCodeDraft('dataset:b', 'same', 'same');
  expect(hasCodeDrafts()).toBe(false);
  keepCodeDraft('dataset:c', 'y', ''); keepCodeDraft('dataset:d', 'z', '');
  clearCodeDrafts(key => key === 'dataset:d');
  expect(codeDraftKeys()).toEqual(['dataset:d']);
  clearCodeDrafts();
  expect(hasCodeDrafts()).toBe(false);
});
