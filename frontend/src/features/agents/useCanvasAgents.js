import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../../api.js';
export const chatNodeId = id => `chat:${id}`;
export const memoryId = m => m.memory_id || `mem:space:${m.space_id}`;
export const memoryBinding = id => id.startsWith('mem:space:') ? { space_id: id.slice(10) } : { memory_id: id };
export const isChatNode = id => id?.startsWith('chat:');
export function useCanvasAgents(graphId, reportError) {
  const currentGraph = useRef(graphId);
  currentGraph.current = graphId;
  const adding = useRef(false);
  const [agents, setAgents] = useState([]);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let active = true;
    setAgents([]); setLoaded(false);
    if (graphId && api.canvasAgents) api.canvasAgents(graphId).then(r => { if (active) { setAgents(r.agents); setLoaded(true); } }).catch(e => { if (active) reportError(e); });
    return () => { active = false; };
  }, [graphId, reportError]);
  const latest = useRef(agents);
  latest.current = agents;
  const saves = useRef(new Map());
  const [pendingSaves, setPendingSaves] = useState({});
  const save = useCallback((agent, patch) => {
    const queueKey = `${graphId}:${agent.id}`;
    setPendingSaves(counts => ({ ...counts, [queueKey]: (counts[queueKey] || 0) + 1 }));
    const previous = saves.current.get(queueKey) || Promise.resolve();
    const pending = previous.catch(() => {}).then(async () => {
      if (currentGraph.current !== graphId) throw new Error('Task changed before the connection was saved');
      const current = latest.current.find(a => a.id === agent.id) || agent;
      const changes = typeof patch === 'function' ? patch(current) : patch;
      const { id, ...settings } = { ...current, ...changes };
      const saved = await api.updateCanvasAgent(graphId, id, settings);
      if (currentGraph.current === graphId) {
        latest.current = latest.current.map(a => a.id === id ? saved : a);
        setAgents(latest.current);
      }
      return saved;
    });
    saves.current.set(queueKey, pending);
    pending.finally(() => { setPendingSaves(counts => ({ ...counts, [queueKey]: Math.max(0, (counts[queueKey] || 1) - 1) })); if (saves.current.get(queueKey) === pending) saves.current.delete(queueKey); }).catch(() => {});
    return pending;
  }, [graphId]);
  const restore = useCallback(async agents => {
    await Promise.all([...saves.current.values()]);
    if (currentGraph.current !== graphId) throw new Error('Task changed before restoring canvas');
    const result = await api.restoreCanvasAgents(graphId, agents);
    if (currentGraph.current === graphId) { latest.current = result.agents; setAgents(result.agents); }
  }, [graphId]);
  const remove = useCallback(async id => {
    await api.removeCanvasAgent(graphId, id);
    if (currentGraph.current === graphId) setAgents(list => list.filter(a => a.id !== id));
  }, [graphId]);
  const add = async () => {
    if (adding.current) throw new Error('Agent creation is already in progress');
    adding.current = true;
    setBusy(true);
    try {
      const agent = await api.createCanvasAgent(graphId, { name: `Chat Agent ${agents.length + 1}`, x: 400 + agents.length * 40, y: 250 });
      if (currentGraph.current === graphId) setAgents(list => [...list, agent]);
      return agent;
    } finally { adding.current = false; setBusy(false); }
  };
  const nodes = useMemo(() => agents.map(a => ({ id: chatNodeId(a.id), type: 'chatAgent', width: 220, height: 88, position: { x: a.x, y: a.y }, data: { title: a.name, agent: a }, deletable: false })), [agents]);
  const edges = useMemo(() => agents.flatMap(a => a.memories.flatMap(m => [
    ...(m.read ? [{ id: `chat-read:${a.id}:${memoryId(m)}`, source: memoryId(m), target: chatNodeId(a.id), sourceHandle: m.read_source_handle || 's-out', targetHandle: m.read_target_handle || 'in', label: 'reads', data: { chatAgent: a.id, agent: chatNodeId(a.id), resource: memoryId(m), memory: 'read', space: memoryId(m), direction: 'read' } }] : []),
    ...(m.write ? [{ id: `chat-write:${a.id}:${memoryId(m)}`, source: chatNodeId(a.id), target: memoryId(m), sourceHandle: m.write_source_handle || 'out', targetHandle: m.write_target_handle || 's-in', label: 'writes', data: { chatAgent: a.id, agent: chatNodeId(a.id), resource: memoryId(m), memory: 'write', space: memoryId(m), direction: 'write' } }] : []),
  ])), [agents]);
  return { agents, loaded, nodes, edges, add, save, remove, restore, busy, setAgents, isSaving: id => !!pendingSaves[`${graphId}:${id}`] };
}
