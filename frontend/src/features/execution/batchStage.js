/**
 * How the canvas reads a batch: node by node, or record by record.
 *
 * A node-major batch runs one node over the whole wave of records before the
 * next node starts, so at any moment exactly one node is running — the one
 * the server names in `stage`. The canvas used to infer each node's status
 * from the per-node aggregate, which in that mode lights every node that has
 * ever seen a record: "everything running at once", which is what nobody was
 * doing. A record-major batch (and every batch saved before `mode` existed,
 * which has none) keeps that aggregate: there each node really does hold as
 * many records as there are workers.
 */
import { compactTokens } from '../../components/tokenUsageText.js';

// No `mode` means a batch written before node-major execution: record-major.
export const isNodeMajor = (batch) => batch?.mode === 'node';

// The wave being run right now, or nothing — a settled batch has no stage,
// and then the aggregate is the whole truth again.
export const batchStage = (batch) =>
  (isNodeMajor(batch) && batch?.stage?.node ? batch.stage : null);

/**
 * How far along the workflow each node sits.
 *
 * Kahn's algorithm over the edges, ties broken by the order the canvas
 * already has; nodes left in a cycle keep that order. This is what says which
 * nodes are behind the running one and which are still ahead of it — `stage`
 * names only one node, not the shape of the graph.
 */
export function nodeOrder(nodes, edges) {
  const ids = (nodes || []).map((n) => n.id);
  const known = new Set(ids);
  const waiting = new Map(ids.map((id) => [id, 0]));
  const after = new Map(ids.map((id) => [id, []]));
  (edges || []).forEach((e) => {
    if (!known.has(e.source) || !known.has(e.target) || e.source === e.target) return;
    after.get(e.source).push(e.target);
    waiting.set(e.target, waiting.get(e.target) + 1);
  });
  const rank = {};
  const ready = ids.filter((id) => !waiting.get(id));
  const queued = new Set(ready);
  let next = 0;
  while (ready.length) {
    const id = ready.shift();
    rank[id] = next++;
    after.get(id).forEach((target) => {
      waiting.set(target, waiting.get(target) - 1);
      if (!waiting.get(target) && !queued.has(target)) {
        queued.add(target);
        ready.push(target);
      }
    });
  }
  ids.forEach((id) => { if (rank[id] == null) rank[id] = next++; });
  return rank;
}

// "node 3/8 · detect · 120/275" — where the batch is, in the terms it runs in.
export function stageText(batch) {
  const stage = batchStage(batch);
  if (!stage) return null;
  const where = stage.index && stage.of ? `node ${stage.index}/${stage.of}` : 'node';
  const wave = stage.total != null ? ` · ${stage.done ?? 0}/${stage.total}` : '';
  return `${where} · ${stage.node}${wave}`;
}

// "120/275" on the node itself.
export const stageBadge = (stage) => `${stage.done ?? 0}/${stage.total ?? '?'}`;

const tokens = (usage) => (usage?.reported_calls ? ` · ${compactTokens(usage)}` : '');

// The per-node aggregate: records that have passed through this node.
function aggregate(p) {
  const total = p.completed + p.running + p.failed + p.pending;
  return {
    runStatus: p.running > 0 ? 'running'
      : p.failed > 0 ? 'failed'
        : total > 0 && p.completed === total ? 'completed'
          : 'pending',
    batchBadge: `${p.completed}/${total}${p.failed ? ` ·${p.failed}✗` : ''}${tokens(p.token_usage)}`,
  };
}

/**
 * Each node's status and badge for the batch on the canvas, or null when the
 * batch says nothing yet (the caller then falls back to the single run).
 */
export function batchNodeStates(batch, nodes, edges) {
  const progress = batch?.node_progress || null;
  const stage = batchStage(batch);
  if (!stage && !progress) return null;
  const rank = stage ? nodeOrder(nodes, edges) : null;
  // A stage naming a node the canvas does not have (renamed since, or a node
  // the server added) still places the wave: `index` is 1-based.
  const here = stage ? (rank[stage.node] ?? (stage.index || 1) - 1) : 0;
  const states = {};
  (nodes || []).forEach((n) => {
    const p = progress?.[n.id] || null;
    if (!stage) {
      states[n.id] = p ? aggregate(p) : { runStatus: 'pending', batchBadge: null };
      return;
    }
    if (n.id === stage.node) {
      states[n.id] = {
        runStatus: 'running',
        batchBadge: `${stageBadge(stage)}${p?.failed ? ` ·${p.failed}✗` : ''}${tokens(p?.token_usage)}`,
      };
      return;
    }
    const behind = rank[n.id] < here;
    states[n.id] = {
      runStatus: behind ? (p?.failed ? 'failed' : 'completed') : 'pending',
      batchBadge: p ? aggregate(p).batchBadge : null,
    };
  });
  return states;
}
