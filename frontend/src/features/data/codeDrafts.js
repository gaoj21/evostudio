// Code editors in the Inspector whose text has not been confirmed into the
// node yet. That text is not part of the canvas, so the canvas's own unsaved
// state cannot see it; Studio asks here before an action would drop it.
//
// A draft registered with text (keepCodeDraft) outlives its editor: the
// Inspector unmounts when the right pane shows Chat or collapses, and the
// editor reads the draft back when it mounts again for the same node. It ends
// only when it is confirmed or discarded, its node goes away, or Studio drops
// it (clearCodeDrafts) after the user agreed to lose it.
const open = new Map();
const listeners = new Set();

function put(key, value) {
  const had = open.has(key);
  if (value) open.set(key, value); else open.delete(key);
  if (had !== !!value) listeners.forEach((fn) => fn(open.size > 0));
}

// An editor without a lasting key: dirty while it is on screen.
export function setCodeDraftOpen(key, dirty) {
  put(key, dirty ? (open.get(key) || true) : null);
}

// A draft that lasts across remounts. `base` is the confirmed code it was
// edited from, so a remount can tell whether the confirmed code moved since.
export function keepCodeDraft(key, text, base) {
  put(key, text == null || text === (base || '') ? null : { text, base: base || '' });
}

export function getCodeDraft(key) {
  const value = open.get(key);
  return value && typeof value === 'object' ? value : null;
}

// Drop drafts the user agreed to lose; `keep(key)` spares those still wanted.
export function clearCodeDrafts(keep = () => false) {
  [...open.keys()].forEach((key) => { if (!keep(key)) put(key, null); });
}

// A renamed node keeps its draft under its new key.
export function moveCodeDraft(from, to) {
  const value = getCodeDraft(from);
  if (!value || from === to) return;
  put(from, null);
  put(to, value);
}

export const codeDraftKeys = () => [...open.keys()];

export const hasCodeDrafts = () => open.size > 0;

export function onCodeDraftsChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export const datasetDraftKey = (nodeId) => `dataset:${nodeId}`;

export const CODE_DRAFT_WARNING = 'There is unconfirmed Dataset code in the Inspector. Discard it?';
