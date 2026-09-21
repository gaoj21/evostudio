import { expect, it } from 'vitest';
import { memoryOverlay, flowToGraph } from '../canvas/convert.js';
import { connectMemory, disconnectMemory, removeMemoryReferences, renameMemoryReferences, renameMemoryPositions } from './memoryConnections.js';
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

it('renames a store owner in every read link and saved handle, keeping the read edge', () => {
  const nodes = [agent('a', {}), agent('c', { read_from: ['c', 'a'], canvas_connections: { 'mem:a:read': { sourceHandle: 's-out' }, 'mem:c:write': {} } })];
  const renamed = renameMemoryReferences(nodes.map(n => n.id === 'a' ? { ...n, id: 'b' } : n), 'a', 'b');
  expect(renamed[1].data.memory.read_from).toEqual(['c', 'b']);
  expect(Object.keys(renamed[1].data.memory.canvas_connections)).toEqual(['mem:a:read', 'mem:c:write']);
  const reads = memoryOverlay(renamed).memEdges.filter(e => e.data.memory === 'read' && e.data.agent === 'c').map(e => e.data.from);
  expect(reads).toEqual(['c', 'b']);
  expect(renameMemoryReferences(nodes, 'a', 'a')).toBe(nodes);
});
it('moves a renamed store position and leaves other positions alone', () => {
  expect(renameMemoryPositions({ 'mem:a': { x: 1, y: 2 }, 'mem:space:s1': { x: 3, y: 4 } }, 'a', 'b'))
    .toEqual({ 'mem:a': { x: 1, y: 2 }, 'mem:space:s1': { x: 3, y: 4 } });
  const untouched = { 'mem:z': { x: 0, y: 0 } };
  expect(renameMemoryPositions(untouched, 'a', 'b')).toBe(untouched);
});


it('renames qualified bindings and preserves the physical store across repeated renames', () => {
  const nodes = [agent('new', { at: 'nodes.old.outputs.when' }), agent('reader', { match: 'nodes.old.outputs.id', context: ['nodes.old.outputs.extra'], key: 'nodes.old.outputs.key' })];
  const result = renameMemoryReferences(nodes, 'old', 'new');
  expect(result[0].data.memory.store_id).toBe('old');
  expect(result[0].data.memory.at).toBe('nodes.new.outputs.when');
  expect(result[1].data.memory.match).toBe('nodes.new.outputs.id');
  expect(result[1].data.memory.key).toBe('nodes.new.outputs.key');
  expect(result[1].data.memory.context).toEqual(['nodes.new.outputs.extra']);
  const again = renameMemoryReferences(result.map(n => n.id === 'new' ? { ...n, id: 'third' } : n), 'new', 'third');
  expect(again[0].data.memory.store_id).toBe('old');
});
