// Chat Agents are workflow nodes of kind "chat_agent" (see convert.js): saved,
// undone and edited with the rest of the canvas. They do not run; they are
// chatted with on their own, and their conversations are keyed by the node's
// `agent_id`, which never changes when the node is renamed.
import { uniqueName } from '../canvas/convert.js';

export const CHAT_AGENT = 'chat_agent';
export const memoryId = m => m.memory_id || `mem:space:${m.space_id}`;
export const memoryBinding = id => id.startsWith('mem:space:') ? { space_id: id.slice(10) } : { memory_id: id };
export const isChatAgent = node => node?.data?.kind === CHAT_AGENT;

// 32 hex digits, as the backend expects (uuid4().hex).
export function newAgentId() {
  const bytes = new Uint8Array(16);
  if (globalThis.crypto?.getRandomValues) globalThis.crypto.getRandomValues(bytes);
  else for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256);
  return Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
}

export const CHAT_AGENT_DEFAULTS = {
  kind: CHAT_AGENT,
  description: '',
  instructions: 'You are a helpful research assistant.',
  provider: null,
  max_steps: 20,
  timeout: 300,
  toolkits: null,
  tools: null,
  skill_names: [],
  memories: [],
};

// A new Chat Agent node named after the ones already on the canvas.
export function newChatAgentNode(nodes, position) {
  const taken = new Set(nodes.map(n => n.id));
  const count = nodes.filter(isChatAgent).length;
  let n = count + 1;
  while (taken.has(`chat_agent_${n}`)) n += 1;
  const name = uniqueName(`chat_agent_${n}`, taken);
  return {
    id: name,
    type: 'chatAgent',
    position: position || { x: 400 + count * 40, y: 250 },
    data: { ...CHAT_AGENT_DEFAULTS, agent_id: newAgentId() },
  };
}

// The "reads"/"writes" lines between memories and the Chat Agents bound to
// them, drawn from each node's `memories`.
export function chatAgentEdges(nodes) {
  return nodes.filter(isChatAgent).flatMap(a => (a.data.memories || []).flatMap(m => [
    ...(m.read ? [{ id: `chat-read:${a.id}:${memoryId(m)}`, source: memoryId(m), target: a.id, sourceHandle: m.read_source_handle || 's-out', targetHandle: m.read_target_handle || 'in', label: 'reads', data: { chatAgent: a.id, agent: a.id, resource: memoryId(m), memory: 'read', space: memoryId(m), direction: 'read' } }] : []),
    ...(m.write ? [{ id: `chat-write:${a.id}:${memoryId(m)}`, source: a.id, target: memoryId(m), sourceHandle: m.write_source_handle || 'out', targetHandle: m.write_target_handle || 's-in', label: 'writes', data: { chatAgent: a.id, agent: a.id, resource: memoryId(m), memory: 'write', space: memoryId(m), direction: 'write' } }] : []),
  ]));
}

// The node's memories with one direction of `resourceId` switched on (a
// connect gesture) or off (a removed line).
export function setMemory(memories, resourceId, direction, on, handles = {}) {
  const current = memories || [];
  const previous = current.find(m => memoryId(m) === resourceId) || { ...memoryBinding(resourceId), read: false, write: false };
  const next = { ...previous, [direction]: on, ...(on ? handles : {}) };
  return [...current.filter(m => memoryId(m) !== resourceId), ...(next.read || next.write ? [next] : [])];
}
