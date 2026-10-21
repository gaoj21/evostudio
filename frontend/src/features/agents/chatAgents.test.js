import { describe, expect, it } from 'vitest';
import { graphToFlow, flowToGraph } from '../canvas/convert.js';
import { chatAgentEdges, isChatAgent, newAgentId, newChatAgentNode, setMemory } from './chatAgents.js';

const AGENT = { name: 'research_chat', kind: 'chat_agent', agent_id: 'a'.repeat(32), description: 'Asks around',
  instructions: 'Be brief.', provider: null, max_steps: 7, timeout: 300, toolkits: null, tools: ['double'],
  skill_names: ['rubric'], memories: [{ space_id: 'b'.repeat(32), read: true, write: true, read_source_handle: 's-out-left' }],
  x: 12, y: 34 };

describe('Chat Agent nodes', () => {
  it('round-trip through the canvas unchanged, as chatAgent nodes without workflow fields', () => {
    const graph = { id: 'g', name: 'G', goal: '', tasks: [{ name: 'a', inputs: [], outputs: [] }, AGENT], edges: [] };
    const { nodes, edges } = graphToFlow(graph);
    const node = nodes[1];
    expect(node).toMatchObject({ id: 'research_chat', type: 'chatAgent', position: { x: 12, y: 34 } });
    expect(isChatAgent(node)).toBe(true);
    expect(node.data.prompt).toBeUndefined();
    expect(flowToGraph(graph, nodes, edges).tasks[1]).toEqual(AGENT);
  });

  it('draw read and write lines from their memories, on the handles chosen', () => {
    const { nodes } = graphToFlow({ tasks: [AGENT], edges: [] });
    const lines = chatAgentEdges(nodes);
    expect(lines.map(e => [e.id, e.source, e.target, e.sourceHandle])).toEqual([
      [`chat-read:research_chat:mem:space:${'b'.repeat(32)}`, `mem:space:${'b'.repeat(32)}`, 'research_chat', 's-out-left'],
      [`chat-write:research_chat:mem:space:${'b'.repeat(32)}`, 'research_chat', `mem:space:${'b'.repeat(32)}`, 'out'],
    ]);
  });

  it('are created with a name of their own and a fresh agent_id', () => {
    const first = newChatAgentNode([{ id: 'chat_agent_1', data: {} }]);
    expect(first.id).toBe('chat_agent_2');
    expect(first.data).toMatchObject({ kind: 'chat_agent', max_steps: 20, memories: [] });
    expect(first.data.agent_id).toMatch(/^[0-9a-f]{32}$/);
    expect(newAgentId()).not.toBe(newAgentId());
  });

  it('switch one direction of a memory on and off', () => {
    const on = setMemory([], 'mem:investigate', 'read', true, { read_source_handle: 's-out' });
    expect(on).toEqual([{ memory_id: 'mem:investigate', read: true, write: false, read_source_handle: 's-out' }]);
    expect(setMemory(on, 'mem:investigate', 'read', false)).toEqual([]);
  });
});
