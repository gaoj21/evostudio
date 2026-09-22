/**
 * Pressing Stop and having nothing stop.
 *
 * The badge said "Stopping…" whatever the server answered, so a declined
 * request was indistinguishable from a successful one — the batch kept
 * running while the UI showed it winding down. These pin the difference.
 */
import { describe, expect, it } from 'vitest';

import { cancelOutcome, unattendedBatch, unfinishedCount, resumeLabel } from './batchControl.js';

describe('cancelOutcome', () => {
  it('says a batch is stopping when the server agreed', () => {
    const { stopping, message } = cancelOutcome({
      cancelled: true, not_started: 148, interrupted: 0, finished: 44,
    });

    expect(stopping).toBe(true);
    expect(message).toBeNull();
  });

  it('says what is spared and what is being interrupted', () => {
    // Stop means stop: the records in flight are cut off, not left to finish.
    const { stopping, message } = cancelOutcome({
      cancelled: true, not_started: 148, interrupted: 5, finished: 44,
    });

    expect(stopping).toBe(true);
    expect(message).toContain('148 record(s) will not be started');
    expect(message).toContain('5 in progress are being interrupted');
    expect(message).not.toMatch(/cannot be interrupted|will finish/);
  });

  it('does not claim to be stopping when the server declined', () => {
    // This is the bug: the batch kept going and the UI showed "Stopping…".
    const { stopping, message } = cancelOutcome({
      cancelled: false, reason: 'batch already interrupted',
    });

    expect(stopping).toBe(false);
    expect(message).toContain('was not stopped');
    expect(message).toContain('batch already interrupted');
  });

  it('tells you it may still be running, since it probably is', () => {
    const { message } = cancelOutcome({ cancelled: false, reason: 'no such batch' });
    expect(message).toContain('may still be running');
  });

  it('still speaks up when the server gives no reason', () => {
    expect(cancelOutcome({ cancelled: false }).message).toContain('the server declined');
  });

  it('treats a missing response as a failure to stop, not a success', () => {
    // A response that never arrived must never read as "stopped".
    expect(cancelOutcome(undefined).stopping).toBe(false);
    expect(cancelOutcome(null).stopping).toBe(false);
    expect(cancelOutcome({}).stopping).toBe(false);
  });
});

describe('unattendedBatch', () => {
  const running = { batch_id: '5f9c09fc65f5', status: 'running' };
  const cancelled = { batch_id: 'a8f9e95f59e7', status: 'cancelled' };

  it('finds the batch still running while you look at another', () => {
    // The exact situation: Stop was bound to the cancelled one for thirteen
    // clicks while this one ran to completion.
    expect(unattendedBatch([running, cancelled], 'a8f9e95f59e7'))
      .toBe('5f9c09fc65f5');
  });

  it('says nothing when the batch you are looking at is the live one', () => {
    expect(unattendedBatch([running, cancelled], '5f9c09fc65f5')).toBeNull();
  });

  it('counts a cancelling batch as live, because it is still spending', () => {
    expect(unattendedBatch([{ batch_id: 'x', status: 'cancelling' }], 'y')).toBe('x');
  });

  it('ignores batches that have settled', () => {
    expect(unattendedBatch([cancelled, { batch_id: 'z', status: 'completed' }], 'q'))
      .toBeNull();
  });

  it('copes with nothing to look at', () => {
    expect(unattendedBatch(undefined, 'a')).toBeNull();
    expect(unattendedBatch([], 'a')).toBeNull();
  });
});


describe('what a resume would run', () => {
  it('counts every record that did not succeed, from the items when loaded', () => {
    const batch = { status: 'cancelled', items: [
      { status: 'success' }, { status: 'cancelled' }, { status: 'failed' }, { status: 'pending' }] };
    expect(unfinishedCount(batch)).toBe(3);
    expect(resumeLabel(batch)).toBe('Resume (3 left)');
  });

  it('works from a listing row, which has counts but no items', () => {
    expect(unfinishedCount({ status: 'interrupted', total: 42, counts: { success: 15, failed: 1, cancelled: 26 } }))
      .toBe(27);
  });

  it('offers nothing for a batch that is running or fully done', () => {
    expect(resumeLabel({ status: 'running', total: 5, counts: { success: 2 } })).toBeNull();
    expect(resumeLabel({ status: 'succeeded', total: 5, counts: { success: 5 } })).toBeNull();
  });
});


describe('a stream that stopped before reading everything', () => {
  it('can be resumed even when every record read so far finished', () => {
    expect(resumeLabel({ status: 'cancelled', streaming: true, unread: true, total: 4, counts: { success: 4 } }))
      .toBe('Resume (keep reading)');
  });
  it('says it will run what is left and then keep reading', () => {
    expect(resumeLabel({ status: 'failed', streaming: true, unread: true, total: 4, counts: { success: 3, failed: 1 } }))
      .toBe('Resume (1 left, then keep reading)');
  });
  it('offers nothing once the stream was read to the end and everything succeeded', () => {
    expect(resumeLabel({ status: 'succeeded', streaming: true, unread: false, total: 4, counts: { success: 4 } })).toBeNull();
  });
});
