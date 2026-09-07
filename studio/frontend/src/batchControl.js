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
  if (outcome.still_running) {
    // Stopping is not instant: a record inside the framework cannot be
    // interrupted, so say how much is saved and how much is not.
    return {
      stopping: true,
      message: `Stopping: ${outcome.not_started} record(s) will not be started. `
        + `${outcome.still_running} already running cannot be interrupted and will finish.`,
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
