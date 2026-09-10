import { expect, it } from 'vitest';
import { memoryOverlay, flowToGraph } from '../canvas/convert.js';
import { connectMemory, disconnectMemory, removeMemoryReferences } from './memoryConnections.js';
const agent = (id, memory, kind = 'task') => ({ id, position: { x: 0, y: 0 }, data: {
  kind, use_long_term_memory: !!memory, memory, inputs: [], outputs: [],
} });
const resource = { id: 'mem:space:s1', data: { kind: 'mem0', space_id: 's1', title: 'Research' } };
it('deduplicates a shared space and draws separate read and write directions', () => {
  const { memNodes, memEdges } = memoryOverlay([
    agent('a', { provider: 'mem0', space_id: 's1', read_enabled: false }),
    agent('b', { provider: 'mem0', space_id: 's1', write_enabled: false }),
  ], { resources: [{ space_id: 's1', name: 'Research' }] });
  expect(memNodes).toHaveLength(1);
  expect(memNodes[0].data).toMatchObject({ title: 'Research', readers: ['b'], writers: ['a'] });
  expect(memEdges.map(e => [e.source, e.target])).toEqual([['a', 'mem:space:s1'], ['mem:space:s1', 'b']]);
});
it('connects reads without writes and writes without reads', () => {
  const read = connectMemory([agent('a')], resource, 'a', 'read')[0].data.memory;
  expect(read).toMatchObject({ read_enabled: true, write_enabled: false, space_id: 's1' });
  const write = connectMemory([agent('a')], resource, 'a', 'write')[0].data.memory;
  expect(write).toMatchObject({ read_enabled: false, write_enabled: true });
});
it('disconnects only the selected direction and keeps the resource', () => {
  let nodes = connectMemory([agent('a')], resource, 'a', 'read');
  nodes = connectMemory(nodes, resource, 'a', 'write');
  nodes = disconnectMemory(nodes, memoryOverlay(nodes).memEdges.find(e => e.data.memory === 'read'));
  expect(nodes[0].data.memory).toMatchObject({ read_enabled: false, write_enabled: true });
  expect(memoryOverlay(nodes).memNodes).toHaveLength(1);
});
it('rejects unsupported nodes and does not silently replace active memory', () => {
  expect(() => connectMemory([agent('tool', undefined, 'tool')], resource, 'tool', 'read')).toThrow('Agent nodes only');
  expect(() => connectMemory([agent('a', { provider: 'mem0', space_id: 'other' })], resource, 'a', 'write')).toThrow('Disconnect');
});
it('restores a usable recall count when reconnecting', () => {
  const nodes = connectMemory([agent('a', { provider: 'mem0', space_id: 's1', retrieve: 0 })], resource, 'a', 'read');
  expect(nodes[0].data.memory.retrieve).toBe(3);
});
it('keeps disconnected resources and positions in the saved graph', () => {
  const meta = { id: 'g', memory_resources: [{ space_id: 's1', name: 'Research' }], memory_positions: { 'mem:space:s1': { x: 80, y: 90 } } };
  const saved = flowToGraph(meta, [], []);
  const overlay = memoryOverlay([], { resources: saved.memory_resources, positions: saved.memory_positions });
  expect(overlay.memNodes[0].position).toEqual({ x: 80, y: 90 });
  expect(saved.tasks).toEqual([]); expect(saved.edges).toEqual([]);
});
it('removes dangling legacy reads when an Agent is deleted', () => {
  const result = removeMemoryReferences([agent('a', {}), agent('b', { read_from: ['a'] })], ['a']);
  expect(result[0].data.memory).toMatchObject({ read_from: [], read_enabled: false });
});

it('preserves workflow Memory handle positions through serialization', () => {
  let nodes = connectMemory([agent('a')], resource, 'a', 'read', { sourceHandle: 's-out-left', targetHandle: 't-in' });
  nodes = connectMemory(nodes, resource, 'a', 'write', { sourceHandle: 'out', targetHandle: 's-in-right' });
  const saved = flowToGraph({ id: 'g' }, nodes, []);
  const policy = saved.tasks[0].memory;
  expect(policy.canvas_connections['mem:space:s1:read']).toEqual({ sourceHandle: 's-out-left', targetHandle: 't-in' });
  const edges = memoryOverlay(nodes).memEdges;
  expect(edges.find(e => e.data.memory === 'read')).toMatchObject({ sourceHandle: 's-out-left', targetHandle: 't-in' });
  expect(edges.find(e => e.data.memory === 'write')).toMatchObject({ sourceHandle: 'out', targetHandle: 's-in-right' });
});
