/**
 * Pressing Stop and having nothing stop.
 *
 * The badge said "Stopping…" whatever the server answered, so a declined
 * request was indistinguishable from a successful one — the batch kept
 * running while the UI showed it winding down. These pin the difference.
 */
import { describe, expect, it } from 'vitest';

import { cancelOutcome, unattendedBatch } from './batchControl.js';

describe('cancelOutcome', () => {
  it('says a batch is stopping when the server agreed', () => {
    const { stopping, message } = cancelOutcome({
      cancelled: true, not_started: 148, still_running: 0, finished: 44,
    });

    expect(stopping).toBe(true);
    expect(message).toBeNull();
  });

  it('says how much is saved and how much cannot be', () => {
    // Stopping is not instant: a record inside the framework runs to the end.
    const { stopping, message } = cancelOutcome({
      cancelled: true, not_started: 148, still_running: 5, finished: 44,
    });

    expect(stopping).toBe(true);
    expect(message).toContain('148 record(s) will not be started');
    expect(message).toContain('5 already running cannot be interrupted');
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
