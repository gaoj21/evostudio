/**
 * Keyboard shortcuts that are not undo/save, named so the handler in App
 * reads as intent rather than key codes.
 */

// ⌘⇧M / Ctrl+Shift+M: snapshot memory, then clear it — the step before a
// measured run. Not while typing, not while a run is on screen.
export function isResetMemoryShortcut(e) {
  return !!(e.metaKey || e.ctrlKey) && e.shiftKey && (e.key === 'M' || e.key === 'm');
}

// The label on the menu item, in the platform's own notation.
export const RESET_MEMORY_KEYS = typeof navigator !== 'undefined'
  && /Mac|iPhone|iPad/.test(navigator.platform || '') ? '⌘⇧M' : 'Ctrl+Shift+M';

/** What the notice says once a reset has happened. */
export function describeMemoryAction(out) {
  const kb = (n) => (n >= 1024 * 1024 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.round(n / 1024)} KB`);
  const parts = Object.entries(out.sizes || {})
    .filter(([, n]) => n > 0).map(([store, n]) => `${store} ${kb(n)}`);
  const what = parts.length ? parts.join(', ') : 'nothing was stored';
  const which = out.graph_id ? ` for ${out.graph_id}` : '';
  if (!out.cleared) {
    return out.saved
      ? `Memory backed up${which} to ${out.backup} (${what}); memory is unchanged.`
      : `Nothing to back up${which}: ${what}.`;
  }
  return `Memory cleared${which}; ${out.backup ? `copy kept at ${out.backup}` : 'no copy was kept'} (${what}).`;
}

// Kept for callers that only clear.
export const describeReset = describeMemoryAction;

