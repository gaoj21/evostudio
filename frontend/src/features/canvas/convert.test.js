import { connectEdge, memoryOverlay } from './convert.js';
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
    // `enabled` is written out on every task now; it was implied before.
    expect(back.tasks[0]).toEqual({ ...graph.tasks[0], enabled: true });
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
          source: { type: 'project_feed', split: 'dev', n: 2, seed: 7 },
          outputs: [{ name: 'subject', type: 'str', description: 'c', required: true }],
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

  it('never invents an Input type for a source saved without one', () => {
    const { nodes } = graphToFlow({ ...graph, tasks: [{ name: 'input', kind: 'source' }] });
    expect(nodes[0].data.source).toEqual({});
    const back = flowToGraph({ id: 'g1', name: 'Graph' }, [{ ...nodes[0], data: { ...nodes[0].data, source: undefined } }], []);
    expect(back.tasks[0].source).toEqual({});
    expect(JSON.stringify(back)).not.toMatch(/credit_risk/);
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


describe('edges carry what crosses them', () => {
  const task = (name, inputs = [], outputs = []) => ({
    name, description: '', prompt: 'p', x: 0, y: 0,
    inputs: inputs.map((n) => ({ name: n, type: 'str' })),
    outputs: outputs.map((n) => ({ name: n, type: 'str' })),
  });

  it('a drawn edge maps every same-named output to input', () => {
    const { nodes } = graphToFlow({ name: 'g', goal: '',
      tasks: [task('a', [], ['x', 'y']), task('b', ['y', 'z'], [])], edges: [] });
    const e = connectEdge(nodes, 'a', 'b');
    expect(e.data.mappings).toEqual([{ from: 'y', to: 'y' }]);
    expect(e.data.control_only).toBe(false);
  });

  it('an unmatched edge requires mapping instead of silently becoming order-only', () => {
    const { nodes } = graphToFlow({ name: 'g', goal: '',
      tasks: [task('a', [], ['x']), task('b', ['z'], [])], edges: [] });
    const e = connectEdge(nodes, 'a', 'b');
    expect(e.data.control_only).toBe(false);
    expect(e.label).toBe('map fields');
  });

  it('mappings, control-only and enabled survive the round trip', () => {
    const graph = { name: 'g', goal: '', flow_version: 2,
      tasks: [task('a', [], ['x']), { ...task('b', ['x'], []), enabled: false }, task('c', [], [])],
      edges: [{ source: 'a', target: 'b', mappings: [{ from: 'x', to: 'x' }] },
              { source: 'b', target: 'c', control_only: true }] };
    const { nodes, edges } = graphToFlow(graph);
    const back = flowToGraph({ name: 'g', goal: '' }, nodes, edges);

    expect(back.edges).toEqual([
      { source: 'a', target: 'b', mappings: [{ from: 'x', to: 'x' }] },
      { source: 'b', target: 'c', control_only: true }]);
    expect(back.tasks.map((t) => t.enabled)).toEqual([true, false, true]);
    expect(back.flow_version).toBe(2);
  });
});


describe('implied edges are real but quiet', () => {
  const task = (name, inputs = [], outputs = []) => ({
    name, description: '', prompt: 'p', x: 0, y: 0,
    inputs: inputs.map((n) => ({ name: n, type: 'str' })),
    outputs: outputs.map((n) => ({ name: n, type: 'str' })),
  });

  it('carries the flag through the round trip and draws no label', () => {
    const graph = { name: 'g', goal: '', flow_version: 2,
      tasks: [task('feed', [], ['company']), task('decide', ['company'], [])],
      edges: [{ source: 'feed', target: 'decide',
                mappings: [{ from: 'company', to: 'company' }], implied: true }] };
    const { nodes, edges } = graphToFlow(graph);
    expect(edges[0].className).toContain('edge-implied');
    expect(edges[0].label).toBeUndefined();
    expect(flowToGraph({ name: 'g', goal: '' }, nodes, edges).edges[0].implied).toBe(true);
  });

  it('labels only a rename, since same-name mappings are the norm', () => {
    const { edges } = graphToFlow({ name: 'g', goal: '', flow_version: 2,
      tasks: [task('a', [], ['x', 'finding']), task('b', ['x', 'evidence'], [])],
      edges: [{ source: 'a', target: 'b',
                mappings: [{ from: 'x', to: 'x' }, { from: 'finding', to: 'evidence' }] }] });
    expect(edges[0].label).toBe('finding → evidence');
  });
});


describe('memory on the canvas', () => {
  const node = (id, data, y = 0) => ({ id, position: { x: 0, y }, data });
  const remembering = (id, memory, extra = {}) => node(id, {
    use_long_term_memory: true, memory,
    outputs: [{ name: 'decision' }], inputs: [{ name: 'company' }, { name: 'news' }], ...extra });

  it('draws one store per remembering node, below it, with a write into it', () => {
    const { memNodes, memEdges } = memoryOverlay([remembering('decide', { match: 'company', at: 'as_of' })]);
    expect(memNodes).toHaveLength(1);
    expect(memNodes[0]).toMatchObject({ id: 'mem:decide', type: 'memory',
      position: { x: 0, y: 130 }, data: { owner: 'decide', kind: 'table', match: 'company', at: 'as_of' } });
    expect(memEdges.find((e) => e.id === 'mw:decide')).toMatchObject({ source: 'decide', target: 'mem:decide' });
  });

  it('says what is kept, honouring a narrowed selection', () => {
    const { memNodes } = memoryOverlay([remembering('decide', { match: 'company', outputs: ['decision'], inputs: ['company'] })]);
    expect(memNodes[0].data.keeps).toEqual(['decision', 'company']);
  });

  it('a node reads its own store unless it named another', () => {
    const { memEdges } = memoryOverlay([
      remembering('investigate', { match: 'company' }),
      remembering('decide', { match: 'company', read_from: ['investigate'], retrieve: 3, at: 'as_of' }),
    ]);
    const reads = memEdges.filter((e) => e.data.memory === 'read').map((e) => [e.source, e.target, e.label]);
    expect(reads).toEqual([
      ['mem:investigate', 'investigate', 'reads 3'],
      ['mem:investigate', 'decide', 'reads 3 before as_of'],
    ]);
  });

  it('draws no read for a node that only writes', () => {
    const { memEdges } = memoryOverlay([remembering('log', { retrieve: 0 })]);
    expect(memEdges.map((e) => e.data.memory)).toEqual(['write']);
  });

  it('leaves nodes without memory alone', () => {
    expect(memoryOverlay([node('a', { outputs: [] })])).toEqual({ memNodes: [], memEdges: [] });
  });

  it('shows what the run on screen wrote', () => {
    const { memNodes } = memoryOverlay([remembering('decide', { match: 'company' })], { wrote: { decide: 2 } });
    expect(memNodes[0].data.wrote).toBe(2);
  });
});


describe('memory edges use the vertical ports', () => {
  it('a write drops from the owner\'s bottom into the store\'s top, a read comes straight back', () => {
    const { memEdges } = memoryOverlay([{ id: 'decide', position: { x: 0, y: 0 },
      data: { use_long_term_memory: true, memory: { match: 'company' }, outputs: [], inputs: [] } }]);
    const write = memEdges.find((e) => e.id === 'mw:decide');
    const read = memEdges.find((e) => e.id === 'mr:decide:decide');
    expect([write.sourceHandle, write.targetHandle]).toEqual(['b-out', 's-in']);
    expect([read.sourceHandle, read.targetHandle]).toEqual(['s-out', 'b-in']);
  });
});

it('draws only the enabled memory directions', () => {
  const n = (id, memory) => ({ id, position: { x: 0, y: 0 }, data: {
    use_long_term_memory: true, memory, inputs: [], outputs: [],
  } });
  const { memEdges } = memoryOverlay([
    n('writer', { read_enabled: false }),
    n('reader', { write_enabled: false, read_from: ['writer'] }),
  ]);
  expect(memEdges.map(e => e.id).sort()).toEqual(['mr:writer:reader', 'mw:writer']);
});

it('preserves Deep Agents harness settings through the canvas round trip', () => {
  const graph = { id: 'harness', tasks: [{ name: 'research', inputs: [], outputs: [], harness: { engine: 'deepagents', max_steps: 8, timeout: 120 } }], edges: [] };
  const flow = graphToFlow(graph);
  expect(flowToGraph(graph, flow.nodes, flow.edges).tasks[0].harness).toEqual(graph.tasks[0].harness);
});


it('matches unique case and separator variants but preserves actual field names', () => {
  const nodes = [{ id: 'a', data: { outputs: [{ name: 'Company Name' }] } },
    { id: 'b', data: { inputs: [{ name: 'company_name' }] } }];
  expect(connectEdge(nodes, 'a', 'b').data.mappings).toEqual([{ from: 'Company Name', to: 'company_name' }]);
});

it('does not guess when normalized names are ambiguous', () => {
  const nodes = [{ id: 'a', data: { outputs: [{ name: 'company-name' }, { name: 'Company Name' }] } },
    { id: 'b', data: { inputs: [{ name: 'company_name' }] } }];
  expect(connectEdge(nodes, 'a', 'b').data.mappings).toEqual([]);
});
