// Memory links configure capabilities; they are never workflow dependencies.
export function connectMemory(nodes, resource, agentId, direction, connection = {}) {
  const node = nodes.find(n => n.id === agentId);
  if (!node || (node.data.kind && node.data.kind !== 'task')) throw new Error('Memory connects to Agent nodes only.');
  if (!resource) throw new Error('Select an existing memory resource.');
  const d = node.data, old = d.memory || {}, store = resource.data;
  if (direction === 'write' && store.kind !== 'mem0' && store.owner !== agentId)
    throw new Error('Only the owning Agent can write this legacy store. Use a Mem0 shared space for multiple writers.');
  const active = d.use_long_term_memory && (old.read_enabled !== false || old.write_enabled !== false);
  const mem0 = store.kind === 'mem0';
  const switching = mem0 ? old.provider !== 'mem0' || old.space_id !== store.space_id : old.provider === 'mem0';
  if (active && switching) throw new Error('This Agent already uses another memory backend or space. Disconnect its read and write links first, or change its Memory settings explicitly.');
  if (active && !mem0 && ((old.kind || (old.match ? 'table' : 'recall')) !== store.kind || (store.kind === 'table' && old.match !== store.match)))
    throw new Error('These memory stores use different types or subject keys. Align their settings before connecting.');
  let policy = { ...old };
  if (!active) policy = { ...policy, read_enabled: false, write_enabled: false,
    provider: mem0 ? 'mem0' : 'legacy', kind: mem0 ? 'recall' : store.kind,
    space_id: mem0 ? store.space_id : undefined, match: mem0 ? '' : store.match || '',
    at: mem0 ? '' : store.at || '', read: null, read_from: [],
    context: [...new Set([...(old.context || []), ...(!mem0 ? [store.match, store.at] : [])].filter(Boolean))] };
  if (direction === 'write') policy.write_enabled = true;
  else {
    policy.read_enabled = true;
    if ((policy.retrieve ?? 3) === 0) policy.retrieve = 3;
    policy.read_from = mem0 ? null : [...new Set([...(active ? policy.read_from ?? [agentId] : []), store.owner])];
  }
  policy.canvas_connections = { ...policy.canvas_connections, [`${resource.id}:${direction}`]: {
    sourceHandle: connection.sourceHandle || (direction === 'read' ? 's-out' : 'b-out'),
    targetHandle: connection.targetHandle || (direction === 'read' ? 'b-in' : 's-in'),
  } };
  return nodes.map(n => n.id === agentId ? { ...n, data: { ...d, use_long_term_memory: true, memory: policy } } : n);
}

export function disconnectMemory(nodes, edge) {
  return nodes.map(n => {
    if (n.id !== edge.data?.agent) return n;
    const memory = { ...(n.data.memory || {}) };
    if (edge.data.memory === 'write') memory.write_enabled = false;
    else if (memory.provider === 'mem0') memory.read_enabled = false;
    else {
      memory.read_from = (memory.read_from ?? [n.id]).filter(id => id !== edge.data.from);
      if (!memory.read_from.length) memory.read_enabled = false;
    }
    return { ...n, data: { ...n.data, memory } };
  });
}

export function removeMemoryReferences(nodes, removed) {
  return nodes.filter(n => !removed.includes(n.id)).map(n => {
    const policy = n.data.memory;
    if (!policy?.read_from || !policy.read_from.some(id => removed.includes(id))) return n;
    const read_from = policy.read_from.filter(id => !removed.includes(id));
    return { ...n, data: { ...n.data, memory: { ...policy, read_from, ...(read_from.length ? {} : { read_enabled: false }) } } };
  });
}
