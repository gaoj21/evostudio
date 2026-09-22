/**
 * What a stop request actually achieved.
 *
 * The server can decline to stop a batch — it may no longer hold it, or the
 * batch may already have settled. The UI used to render "Stopping…" either
 * way, so a refusal looked exactly like a success: the badge went quiet while
 * the batch carried on spending money. This turns the response into the two
 * things the caller needs to know, so neither can be assumed.
 */
export function cancelOutcome(outcome) {
  if (!outcome?.cancelled) {
    return {
      stopping: false,
      message: `This batch was not stopped: ${outcome?.reason || 'the server declined'}. `
        + 'It may still be running — reopen it from Runs to check.',
    };
  }
  if (outcome.interrupted) {
    // The records in flight are being stopped where they are — the model
    // call each is inside is abandoned, not left to finish.
    return {
      stopping: true,
      message: `Stopping: ${outcome.not_started} record(s) will not be started; `
        + `${outcome.interrupted} in progress are being interrupted.`,
    };
  }
  return { stopping: true, message: null };
}

// A batch is live until it settles; `cancelling` counts, because its
// in-flight records are still running and still cost money.
const LIVE = ['running', 'cancelling', 'pending'];

/**
 * A batch that is still running while you are looking at a different one.
 *
 * The canvas tracks a single batch, and opening an older one from Runs
 * repoints it — badge, progress and Stop all follow. Start a batch, glance at
 * the previous one, and every Stop click goes to the batch you are *reading*
 * rather than the one that is *running*. That happened: thirteen clicks, none
 * of which reached the live batch, which carried on to the end.
 */
export function unattendedBatch(listed, viewedId) {
  const other = (listed || []).find(
    (b) => LIVE.includes(b.status) && b.batch_id !== viewedId);
  return other ? other.batch_id : null;
}


// How many of a batch's records a resume would run. Counted from the items
// when the full batch is loaded, from the listing's counts otherwise; a
// running batch has nothing to resume yet.
export function unfinishedCount(batch) {
  if (!batch || batch.status === 'running' || batch.status === 'cancelling') return 0;
  if (Array.isArray(batch.items) && batch.items.length) {
    return batch.items.filter((i) => i.status !== 'success').length;
  }
  const counts = batch.counts || {};
  const total = batch.total ?? Object.values(counts).reduce((a, b) => a + b, 0);
  return Math.max(0, total - (counts.success || 0));
}

// A streamed batch that stopped before its Dataset was fully read: Resume
// runs what did not finish, then continues reading where it stopped.
function unread(batch) {
  if (!batch || batch.status === 'running' || batch.status === 'cancelling') return false;
  return batch.unread ?? (!!batch.streaming && !batch.collection_complete);
}

export function resumeLabel(batch) {
  const n = unfinishedCount(batch);
  if (unread(batch)) return n ? `Resume (${n} left, then keep reading)` : 'Resume (keep reading)';
  return n ? `Resume (${n} left)` : null;
}
