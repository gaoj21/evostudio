import { describe, expect, it } from 'vitest';

import { flowToGraph, graphToFlow, sluggify, uniqueName } from './convert.js';

/**
 * The canvas graph is the contract between the UI, the backend and an exported
 * project, and it makes a full round trip on every save, import and chat edit.
 * These pin the parts that silently corrupt a workflow when they drift: a node
 * name that stops matching its edges, and a field dropped on the way through.
 */

describe('sluggify', () => {
  it('turns a label into a node name', () => {
    expect(sluggify('CR Source News')).toBe('cr_source_news');
    expect(sluggify('  detect!!  ')).toBe('detect');
  });

  it('never returns an empty name', () => {
    expect(sluggify('!!!')).toBe('node');
    expect(sluggify('')).toBe('node');
  });
});

describe('uniqueName', () => {
  it('leaves a free name alone', () => {
    expect(uniqueName('detect', new Set(['other']))).toBe('detect');
  });

  it('suffixes until it finds a gap', () => {
    expect(uniqueName('detect', new Set(['detect']))).toBe('detect_2');
    expect(uniqueName('detect', new Set(['detect', 'detect_2']))).toBe('detect_3');
  });
});

describe('round trip', () => {
  const graph = {
    id: 'g1',
    name: 'Graph',
    goal: 'a goal',
    output_dir: 'runs',
    preprocess: 'clean_record',
    tasks: [
      {
        name: 'detect',
        description: 'detect things',
        prompt: 'look at {news}',
        system_prompt: 'be careful',
        parse_mode: 'json',
        inputs: [{ name: 'news', type: 'str', description: 'the news', required: true }],
        outputs: [{ name: 'finding', type: 'str', description: 'what was found', required: true }],
        tool_names: ['PythonInterpreterToolkit'],
        skill_names: ['rubric'],
        use_long_term_memory: true,
        save_output: true,
        x: 120,
        y: 40,
      },
    ],
    edges: [],
  };

  it('survives graph -> flow -> graph unchanged', () => {
    const { nodes, edges } = graphToFlow(graph);
    const back = flowToGraph(
      { id: graph.id, name: graph.name, goal: graph.goal, output_dir: graph.output_dir,
        preprocess: graph.preprocess },
      nodes,
      edges,
    );
    expect(back.tasks[0]).toEqual(graph.tasks[0]);
    expect(back.preprocess).toBe('clean_record');
  });

  it('keeps a node id equal to its task name, so edges keep matching', () => {
    const { nodes } = graphToFlow({
      ...graph,
      tasks: [...graph.tasks, { ...graph.tasks[0], name: 'decide' }],
      edges: [{ source: 'detect', target: 'decide' }],
    });
    expect(nodes.map((n) => n.id)).toEqual(['detect', 'decide']);
  });

  it('carries source and tool nodes with their own fields', () => {
    const mixed = {
      ...graph,
      tasks: [
        { name: 'feed', kind: 'source', description: 'a feed',
          source: { type: 'credit_risk', split: 'dev', n: 2, seed: 7 },
          outputs: [{ name: 'company', type: 'str', description: 'c', required: true }],
          save_output: true, x: 0, y: 0 },
        { name: 'wc', kind: 'tool', description: 'count', tool: 'word_count',
          inputs: [{ name: 'text', type: 'str', description: 't', required: true }],
          outputs: [{ name: 'result', type: 'str', description: 'r', required: true }],
          save_output: true, x: 260, y: 0 },
      ],
    };
    const { nodes } = graphToFlow(mixed);
    expect(nodes.map((n) => n.type)).toEqual(['source', 'tool']);

    const back = flowToGraph({ id: 'g1', name: 'Graph', goal: 'a goal', output_dir: 'runs' },
                             nodes, []);
    expect(back.tasks[0].source).toEqual(mixed.tasks[0].source);
    expect(back.tasks[1].tool).toBe('word_count');
  });

  it('fills in defaults for a task saved without them', () => {
    const { nodes } = graphToFlow({ ...graph, tasks: [{ name: 'bare' }] });
    expect(nodes[0].data.parse_mode).toBe('str');
    expect(nodes[0].data.tool_names).toEqual([]);
    expect(nodes[0].data.skill_names).toEqual([]);
    expect(nodes[0].position).toEqual({ x: 0, y: 0 });
  });

  it('carries a node\'s memory selection both ways', () => {
    const graph = {
      name: 'g', goal: '', tasks: [{
        name: 'judge', description: 'd', inputs: [], outputs: [], prompt: 'p',
        use_long_term_memory: true,
        memory: { outputs: ['verdict'], inputs: [], when: 'always', retrieve: 5 },
        x: 0, y: 0,
      }], edges: [],
    };
    const { nodes, edges } = graphToFlow(graph);
    expect(nodes[0].data.memory.outputs).toEqual(['verdict']);
    expect(flowToGraph({ name: 'g', goal: '' }, nodes, edges).tasks[0].memory)
      .toEqual(graph.tasks[0].memory);
  });

  it('leaves the key out entirely for a node that never narrowed it', () => {
    // Absent means "keep everything"; writing null on every task would put one
    // in every saved graph and every export for nothing.
    const graph = {
      name: 'g', goal: '', tasks: [{
        name: 'judge', description: 'd', inputs: [], outputs: [], prompt: 'p', x: 0, y: 0,
      }], edges: [],
    };
    const { nodes, edges } = graphToFlow(graph);
    expect(flowToGraph({ name: 'g', goal: '' }, nodes, edges).tasks[0])
      .not.toHaveProperty('memory');
  });
});
