// A browser mirror of the server's evaluator scratch pad. The server copy is
// the truth; this only exists so code pasted into the panel is never lost
// when the PUT fails (offline, server restarting, request refused).

const key = (graphId) => `evoagentx-studio:evaluator-draft:${graphId}`;

export function readLocalDraft(graphId) {
  if (!graphId) return null;
  try {
    const raw = window.localStorage.getItem(key(graphId));
    return raw ? JSON.parse(raw) : null;
  } catch { return null; }
}

export function writeLocalDraft(graphId, draft) {
  if (!graphId) return;
  try { window.localStorage.setItem(key(graphId), JSON.stringify({ ...draft, saved_at: new Date().toISOString() })); }
  catch { /* private mode or a full store: the server copy still carries it */ }
}

export function clearLocalDraft(graphId) {
  if (!graphId) return;
  try { window.localStorage.removeItem(key(graphId)); }
  catch { /* nothing to do */ }
}

// Which of the two copies to open with: whichever was written last, so
// leaving the panel and coming back shows the newest text either way.
export function newerDraft(server, local) {
  const stamp = (d) => (d?.saved_at ? Date.parse(d.saved_at) || 0 : 0);
  if (!server?.code?.trim()) return local?.code?.trim() ? local : server || local;
  if (!local?.code?.trim()) return server;
  return stamp(local) > stamp(server) ? local : server;
}
