import { describe, expect, it } from 'vitest';
import { batchNodeStates, batchStage, nodeOrder, stageText } from './batchStage.js';

const nodes = [{ id: 'a' }, { id: 'b' }, { id: 'c' }];
const edges = [{ source: 'a', target: 'b' }, { source: 'b', target: 'c' }];

describe('a batch run node by node', () => {
  const batch = { mode: 'node', stage: { node: 'b', index: 2, of: 3, done: 120, total: 275 },
    node_progress: { a: { completed: 275, running: 0, failed: 2, pending: 0 } } };

  it('lights only the node the chunk is on, with its progress', () => {
    const states = batchNodeStates(batch, nodes, edges);

    expect(states.b).toMatchObject({ runStatus: 'running' });
    expect(states.b.batchBadge).toContain('120/275');   // no aggregate for b yet: the stage
    expect(states.a.runStatus).toBe('failed');        // passed, with failures
    expect(states.c.runStatus).toBe('pending');
  });

  it('says where the batch is in node terms', () => {
    expect(stageText(batch)).toBe('node 2/3 · b · 120/275');
  });

  it('counts the running node over the whole batch, not just the DataLoader batch now running', () => {
    // Second DataLoader batch of 16 just arrived: a has done the first 16.
    const second = { mode: 'node', stage: { node: 'a', index: 1, of: 3, done: 0, total: 16 },
      node_progress: { a: { completed: 16, running: 16, failed: 0, pending: 0 },
        b: { completed: 16, running: 0, failed: 0, pending: 16 } } };

    expect(batchNodeStates(second, nodes, edges).a.batchBadge).toMatch(/^16\/32/);
    expect(stageText(second)).toBe('node 1/3 · a · 16/32');
  });

  it('orders nodes by the edges, not by the canvas list', () => {
    const rank = nodeOrder([{ id: 'c' }, { id: 'a' }, { id: 'b' }], edges);
    expect(rank.a < rank.b && rank.b < rank.c).toBe(true);
  });
});

describe('record by record, or an old batch', () => {
  const progress = { a: { completed: 2, running: 1, failed: 0, pending: 1 } };

  it('keeps the per-node aggregate', () => {
    for (const batch of [{ mode: 'record', node_progress: progress }, { node_progress: progress }]) {
      expect(batchStage(batch)).toBeNull();
      expect(stageText(batch)).toBeNull();
      expect(batchNodeStates(batch, nodes, edges).a.runStatus).toBe('running');
    }
  });

  it('a settled node-by-node batch has no stage and falls back to the aggregate', () => {
    const states = batchNodeStates({ mode: 'node', stage: null, node_progress: progress }, nodes, edges);
    expect(states.a.runStatus).toBe('running');
  });
});
