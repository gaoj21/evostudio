// Conversions between the contract's canvas graph JSON and React Flow state.
// Node id === task name (unique, sluggified).

export function sluggify(name) {
  const s = String(name || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '');
  return s || 'node';
}

export function uniqueName(base, taken) {
  const slug = sluggify(base);
  if (!taken.has(slug)) return slug;
  let i = 2;
  while (taken.has(`${slug}_${i}`)) i += 1;
  return `${slug}_${i}`;
}

const DEFAULT_TASK = {
  description: '',
  inputs: [],
  outputs: [],
  prompt: '',
  system_prompt: '',
  parse_mode: 'str',
  tool_names: [],
  skill_names: [],
};

export function taskToNode(task, extra = {}) {
  if (task.kind === 'source') {
    return {
      id: task.name,
      type: 'source',
      position: { x: Number(task.x) || 0, y: Number(task.y) || 0 },
      data: {
        kind: 'source',
        description: task.description || '',
        inputs: [],
        outputs: task.outputs || [],
        source: task.source || { type: 'credit_risk', split: '', n: 1, seed: 42 },
        save_output: task.save_output !== false,
        ...extra,
      },
    };
  }
  if (task.kind === 'tool') {
    return {
      id: task.name,
      type: 'tool',
      position: { x: Number(task.x) || 0, y: Number(task.y) || 0 },
      data: {
        kind: 'tool',
        description: task.description || '',
        tool: task.tool || '',
        inputs: task.inputs || [],
        outputs: task.outputs || [{ name: 'result', type: 'str', description: 'Tool result', required: true }],
        save_output: task.save_output !== false,
        ...extra,
      },
    };
  }
  return {
    id: task.name,
    type: 'task',
    position: { x: Number(task.x) || 0, y: Number(task.y) || 0 },
    data: {
      description: task.description || '',
      inputs: task.inputs || [],
      outputs: task.outputs || [],
      prompt: task.prompt || '',
      system_prompt: task.system_prompt || '',
      parse_mode: task.parse_mode || 'str',
      tool_names: task.tool_names || [],
      skill_names: task.skill_names || [],
      use_long_term_memory: !!task.use_long_term_memory,
      // Which of this node's fields its memory keeps. Absent means "all of
      // them", which is what every node did before the setting existed.
      memory: task.memory || null,
      save_output: task.save_output !== false,
      ...extra,
    },
  };
}

export function graphToFlow(graph) {
  const nodes = (graph.tasks || []).map((t) => taskToNode({ ...DEFAULT_TASK, ...t }));
  const edges = (graph.edges || []).map((e) => ({
    id: `e:${e.source}->${e.target}`,
    source: e.source,
    target: e.target,
  }));
  return { nodes, edges };
}

export function flowToGraph(graphMeta, nodes, edges) {
  return {
    id: graphMeta.id,
    name: graphMeta.name,
    goal: graphMeta.goal,
    output_dir: graphMeta.output_dir || 'runs',
    preprocess: graphMeta.preprocess || null,
    tasks: nodes.map((n) =>
      n.data.kind === 'source'
        ? {
            name: n.id,
            kind: 'source',
            description: n.data.description || '',
            source: n.data.source || { type: 'credit_risk', split: '', n: 1, seed: 42 },
            outputs: n.data.outputs || [],
            save_output: n.data.save_output !== false,
            x: Math.round(n.position.x),
            y: Math.round(n.position.y),
          }
        : n.data.kind === 'tool'
          ? {
              name: n.id,
              kind: 'tool',
              description: n.data.description || '',
              tool: n.data.tool || '',
              inputs: n.data.inputs || [],
              outputs: n.data.outputs || [],
              save_output: n.data.save_output !== false,
              x: Math.round(n.position.x),
              y: Math.round(n.position.y),
            }
          : {
            name: n.id,
            description: n.data.description || '',
            inputs: n.data.inputs || [],
            outputs: n.data.outputs || [],
            prompt: n.data.prompt || '',
            system_prompt: n.data.system_prompt || '',
            parse_mode: n.data.parse_mode || 'str',
            tool_names: n.data.tool_names || [],
            skill_names: n.data.skill_names || [],
            use_long_term_memory: !!n.data.use_long_term_memory,
            // Only when the node has actually narrowed what it keeps: absent
            // means "everything", so writing it out on every task would put
            // a null in every saved graph and every export for nothing.
            ...(n.data.memory ? { memory: n.data.memory } : {}),
            save_output: n.data.save_output !== false,
            x: Math.round(n.position.x),
            y: Math.round(n.position.y),
          }
    ),
    edges: edges.map((e) => ({ source: e.source, target: e.target })),
  };
}
