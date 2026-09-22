import { describe, expect, it } from 'vitest';

import { BATCH_SETTLED, RUN_SETTLED, describeBatch, describeRun, isSettled } from './runStates.js';

describe('one reading of outcomes', () => {
  it('a batch with failed records is never green', () => {
    expect(describeBatch('completed_with_errors').tone).toBe('partial');
    // Batches written by earlier versions say `completed` whatever happened;
    // the counts decide.
    expect(describeBatch('completed', { failed: 2 }).tone).toBe('partial');
    expect(describeBatch('completed', { success: 5 }).tone).toBe('success');
    expect(describeBatch('succeeded').tone).toBe('success');
    expect(describeBatch('failed').tone).toBe('failed');
  });

  it('a stopping batch is not settled, a stopped one is', () => {
    expect(isSettled(BATCH_SETTLED, 'cancelling')).toBe(false);
    expect(isSettled(BATCH_SETTLED, 'cancelled')).toBe(true);
    expect(isSettled(BATCH_SETTLED, 'completed_with_errors')).toBe(true);
  });

  it('a run that was left, lost or interrupted is settled and says what happened', () => {
    for (const status of ['abandoned', 'lost', 'interrupted']) {
      expect(isSettled(RUN_SETTLED, status)).toBe(true);
      expect(describeRun(status).note).toBeTruthy();
      expect(describeRun(status).tone).toBe('stopped');
    }
    expect(isSettled(RUN_SETTLED, 'running')).toBe(false);
  });

  it('copes with a state it has never heard of', () => {
    expect(describeRun('weird').label).toBe('weird');
    expect(describeBatch(undefined).label).toBe('unknown');
  });
});
