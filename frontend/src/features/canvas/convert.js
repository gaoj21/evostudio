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
        // No type until one is chosen in the Inspector: nothing is assumed.
        source: task.source || {},
        save_output: task.save_output !== false,
        ...extra,
      },
    };
  }
  if (task.kind === 'evaluator') return {id:task.name,type:'evaluator',position:{x:Number(task.x)||0,y:Number(task.y)||0},data:{...task,...extra}};
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
      harness: task.harness || null,
      use_long_term_memory: !!task.use_long_term_memory,
      // Which of this node's fields its memory keeps. Absent means "all of
      // them", which is what every node did before the setting existed.
      memory: task.memory || null,
      save_output: task.save_output !== false,
      enabled: task.enabled !== false,
      ...extra,
    },
  };
}

export function graphToFlow(graph) {
  const nodes = (graph.tasks || []).map((t) => taskToNode({ ...DEFAULT_TASK, ...t }));
  const edges = (graph.edges || []).map((e) => edgeToFlow(e));
  return { nodes, edges };
}

// An edge carries what crosses it. Control-only edges only order the two
// nodes and are drawn dashed so the difference is visible on the canvas.
export function edgeToFlow(e) {
  const controlOnly = !!e.control_only;
  // Migration draws the edges the old engine implied; they carry data like
  // any other but are rendered faint and unlabelled, or the canvas of a
  // migrated workflow is a thicket of lines nobody drew.
  const implied = !!e.implied;
  const renamed = (e.mappings || []).filter((m) => m.from !== m.to);
  return {
    id: `e:${e.source}->${e.target}`,
    source: e.source,
    target: e.target,
    data: { mappings: e.mappings || [], control_only: controlOnly, implied },
    // A label only where it says something a glance would not: an order-only
    // edge, or a field renamed across the edge. Same-name mappings are the
    // norm and labelling every one of them is noise.
    label: controlOnly ? 'order only' : !(e.mappings || []).length ? 'map fields'
      : (!implied && renamed.length
        ? renamed.map((m) => `${m.from} → ${m.to}`).join(', ') : undefined),
    className: [controlOnly ? 'edge-control' : '', implied ? 'edge-implied' : '']
      .filter(Boolean).join(' ') || undefined,
  };
}

// Match exact field names first, then unique spelling variants. Ambiguous
// pairs need an explicit choice; absence of a match never means order-only.
export function suggestMappings(outputs = [], inputs = []) {
  const normalize = name => String(name).trim().toLowerCase().replace(/[\s_-]+/g, '');
  const mappings = [];
  for (const input of inputs) {
    const exact = outputs.filter(output => output.name === input.name);
    const similar = outputs.filter(output => normalize(output.name) === normalize(input.name));
    const targetMatches = inputs.filter(other => normalize(other.name) === normalize(input.name));
    const match = exact.length === 1 ? exact[0]
      : similar.length === 1 && targetMatches.length === 1 ? similar[0] : null;
    if (match) mappings.push({ from: match.name, to: input.name });
  }
  return mappings;
}

export function connectEdge(nodes, source, target) {
  const from = nodes.find(n => n.id === source);
  const to = nodes.find(n => n.id === target);
  const mappings = suggestMappings(from?.data?.outputs, to?.data?.inputs);
  return edgeToFlow({ source, target, mappings, control_only: false });
}

export function flowToGraph(graphMeta, nodes, edges) {
  return {
    id: graphMeta.id,
    name: graphMeta.name,
    goal: graphMeta.goal,
    output_dir: graphMeta.output_dir || 'runs',
    preprocess: graphMeta.preprocess || null,
    memory_resources: graphMeta.memory_resources || [],
    memory_positions: graphMeta.memory_positions || {},
    tasks: nodes.map((n) =>
      n.data.kind === 'source'
        ? {
            name: n.id,
            kind: 'source',
            description: n.data.description || '',
            source: n.data.source || {},
            outputs: n.data.outputs || [],
            save_output: n.data.save_output !== false,
            enabled: n.data.enabled !== false,
            x: Math.round(n.position.x),
            y: Math.round(n.position.y),
          }
        : n.data.kind === 'evaluator' ? {name:n.id,kind:'evaluator',description:n.data.description || '',evaluator:n.data.evaluator || {},inputs:n.data.inputs || [],outputs:[],enabled:n.data.enabled!==false,x:Math.round(n.position.x),y:Math.round(n.position.y)}
        : n.data.kind === 'tool'
          ? {
              name: n.id,
              kind: 'tool',
              description: n.data.description || '',
              tool: n.data.tool || '',
              inputs: n.data.inputs || [],
              outputs: n.data.outputs || [],
              save_output: n.data.save_output !== false,
            enabled: n.data.enabled !== false,
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
            ...(n.data.harness ? { harness: n.data.harness } : {}),
            use_long_term_memory: !!n.data.use_long_term_memory,
            // Only when the node has actually narrowed what it keeps: absent
            // means "everything", so writing it out on every task would put
            // a null in every saved graph and every export for nothing.
            ...(n.data.memory ? { memory: n.data.memory } : {}),
            save_output: n.data.save_output !== false,
            enabled: n.data.enabled !== false,
            x: Math.round(n.position.x),
            y: Math.round(n.position.y),
          }
    ),
    edges: edges.map((e) => ({
      source: e.source,
      target: e.target,
      ...(e.data?.control_only
        ? { control_only: true }
        : { mappings: e.data?.mappings || [] }),
      ...(e.data?.implied ? { implied: true } : {}),
    })),
    // Edges above carry explicit mappings: this is a migrated graph.
    flow_version: 2,
  };
}


// ---------------------------------------------------------------------------
// Memory on the canvas
// ---------------------------------------------------------------------------

const MEMORY_PREFIX = 'mem:';
export const isMemoryId = (id) => typeof id === 'string' && id.startsWith(MEMORY_PREFIX);
export const memoryOwner = (id) => (isMemoryId(id) ? id.slice(MEMORY_PREFIX.length) : null);

function keptFields(policy, declared, side) {
  const names = (declared || []).map((p) => p.name).filter(Boolean);
  const chosen = policy[side];
  if (chosen === undefined || chosen === null) return names;      // "all"
  return names.filter((n) => chosen.includes(n));
}

/**
 * The stores and the edges that write to and read from them, derived from
 * the nodes' memory settings. Not part of the saved graph: the settings are
 * the truth and this is how they look. `wrote` is per-owner counts of rows
 * written by the run on screen, when there is one.
 */
export function memoryOverlay(nodes, { wrote = {}, resources = [], positions = {}, spaceNames = {} } = {}) {
  const agents = nodes.filter(n => !n.data?.kind || n.data.kind === 'task');
  const owners = agents.filter(n => n.data?.use_long_term_memory);
  const stores = new Map();
  const memEdges = [];
  const addStore = (id, data, position) => {
    if (!stores.has(id)) stores.set(id, {
      id, type: 'memory', position: positions[id] || position,
      // Derived resources do not persist React Flow measurement events.
      // Supply initial dimensions so re-adoption never hides the node.
      initialWidth: 220, initialHeight: 110,
      draggable: true, selectable: true, deletable: false,
      data: { keeps: [], members: [], readers: [], writers: [], wrote: 0, ...data },
    });
    return stores.get(id);
  };
  resources.forEach((r, i) => addStore(`mem:space:${r.space_id}`, {
    kind: 'mem0', space_id: r.space_id, title: spaceNames[r.space_id] || r.name || 'Shared memory', owner: null,
  }, { x: r.x ?? 360, y: r.y ?? i * 170 }));
  const storeFor = n => {
    const policy = n.data.memory || {};
    if (policy.provider === 'mem0') {
      if (!policy.space_id) return null;
      return addStore(`mem:space:${policy.space_id}`, {
        kind: 'mem0', space_id: policy.space_id, title: spaceNames[policy.space_id] || `Mem0 ${policy.space_id.slice(0, 8)}`, owner: null,
      }, { x: n.position.x + 300, y: n.position.y + 130 });
    }
    return addStore(`mem:${policy.store_id || n.id}`, {
      owner: n.id, title: `${n.id} memory`, kind: policy.kind || (policy.version === 2 || policy.match ? 'table' : 'recall'), version: policy.version, time_filter: policy.time_filter,
      match: policy.match || '', at: policy.at || '',
      keeps: [...keptFields(policy, n.data.outputs, 'outputs'), ...keptFields(policy, n.data.inputs, 'inputs')],
      write_enabled: policy.write_enabled !== false, retrieve: policy.retrieve ?? 3,
    }, { x: n.position.x, y: n.position.y + 130 });
  };
  owners.forEach(storeFor);
  owners.forEach(n => {
    const policy = n.data.memory || {};
    const own = storeFor(n);
    if (own) {
      own.data.members.push(n.id);
      own.data.wrote += wrote[n.id] || 0;
      if (policy.write_enabled !== false) {
        own.data.writers.push(n.id);
        memEdges.push({ id: `mw:${n.id}`, source: n.id, target: own.id,
          sourceHandle: 'b-out', targetHandle: 's-in', ...policy.canvas_connections?.[`${own.id}:write`], label: 'writes', deletable: true,
          className: 'edge-memory edge-memory-write', data: { memory: 'write', agent: n.id, resource: own.id } });
      }
    }
    const retrieve = policy.retrieve ?? 3;
    if (policy.read_enabled === false || retrieve <= 0) return;
    const reads = policy.provider === 'mem0' ? [n.id] : (policy.read_from ?? [n.id]);
    const seen = new Set();
    reads.forEach(name => {
      const source = agents.find(a => a.id === name);
      if (!source) return;
      const resource = storeFor(source);
      if (!resource || seen.has(resource.id)) return;
      seen.add(resource.id); resource.data.readers.push(n.id);
      memEdges.push({ id: `mr:${policy.provider === 'mem0' ? policy.space_id : name}:${n.id}`,
        source: resource.id, target: n.id, sourceHandle: 's-out', targetHandle: 'b-in', ...policy.canvas_connections?.[`${resource.id}:read`],
        label: `reads ${retrieve}${policy.provider !== 'mem0' && policy.at ? ` before ${policy.at}` : ''}`,
        deletable: true, className: 'edge-memory edge-memory-read',
        data: { memory: 'read', agent: n.id, resource: resource.id, from: name } });
    });
  });
  return { memNodes: [...stores.values()], memEdges };
}
