import { describe, expect, it } from 'vitest';
import { carriedFields, edgeFlow, flowEdges, ioSummary, nodeFocus } from './runFlow.js';

const edge = (source, target, mappings = [], extra = {}) =>
  ({ id: `${source}-${target}`, source, target, data: { mappings }, ...extra });

describe('the edges of a live run', () => {
  const statuses = { feed: 'completed', detect: 'running', decide: 'pending', later: 'pending' };
  const statusOf = (id) => statuses[id];
  const edges = [
    edge('feed', 'detect', [{ from: 'news_batch', to: 'news' }, { from: 'as_of', to: 'as_of' }]),
    edge('detect', 'decide', [{ from: 'finding', to: 'finding' }]),
    edge('decide', 'later'),
  ];

  it('marks the input arriving, the output waiting, the rest idle', () => {
    const [into, out, idle] = flowEdges(edges, statusOf, true);

    expect(into.className).toContain('edge-run-in');
    expect(into.animated).toBe(true);
    expect(into.label).toBe('news, as_of →');
    expect(out.className).toContain('edge-run-out');
    expect(out.animated).toBe(false);
    expect(out.label).toBe('→ finding');
    expect(idle.className).toContain('edge-run-idle');
  });

  it('shows finished edges as delivered, without labels', () => {
    const done = flowEdges([edge('a', 'b', [{ from: 'x', to: 'x' }])],
      () => 'completed', true)[0];
    expect(done.className).toContain('edge-run-done');
    expect(done.label).toBeUndefined();
  });

  it('decorates nothing when no run is live', () => {
    const plain = edges[0];
    expect(flowEdges(edges, statusOf, false)[0]).toBe(plain);
    expect(edgeFlow(plain, 'completed', 'running', false)).toBeNull();
  });

  it('keeps a long field list short', () => {
    const many = edge('feed', 'detect', ['a', 'b', 'c', 'd', 'e'].map((n) => ({ from: n, to: n })));
    expect(flowEdges([many], statusOf, true)[0].label).toBe('a, b, c +2 →');
    expect(carriedFields(edge('x', 'y'))).toEqual([]);
  });
});

describe('the nodes of a live run', () => {
  it('focuses the running node and steps the rest back', () => {
    expect(nodeFocus('running', true)).toBe('active');
    expect(nodeFocus('completed', true)).toBe('dim');
    expect(nodeFocus('pending', false)).toBeNull();
  });

  it('says what the working node takes in and puts out', () => {
    expect(ioSummary({ inputs: [{ name: 'news' }, 'as_of'], outputs: [{ name: 'finding' }] }))
      .toEqual({ inputs: 'news, as_of', outputs: 'finding' });
    expect(ioSummary({})).toBeNull();
  });
});
