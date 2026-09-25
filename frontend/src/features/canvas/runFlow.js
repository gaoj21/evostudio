/**
 * What the canvas shows while a workflow runs: which node is working now,
 * and how data moves through it.
 *
 * - The running node is the focus; the rest of the workflow steps back.
 * - An edge into the running node is its input arriving — it flows toward
 *   the node and names the fields it carries.
 * - An edge out of the running node is its output about to leave — it waits,
 *   naming the fields it will carry, and starts flowing once the next node
 *   takes over.
 * - Edges between finished nodes have delivered; edges further ahead are idle.
 *
 * Pure functions over the nodes and edges the canvas already has.
 */

const DONE = new Set(['completed', 'success']);
const LABEL_IN = { fill: 'var(--accent-text)', fontWeight: 600, fontSize: 10.5 };
const LABEL_OUT = { fill: 'var(--text-2)', fontWeight: 500, fontSize: 10.5 };
const LABEL_BG = { fill: 'var(--surface)', stroke: 'var(--accent-border)' };

// The field names an edge carries: what the target receives.
export function carriedFields(edge) {
  const mappings = edge?.data?.mappings || [];
  return mappings.map((m) => m.to || m.from).filter(Boolean);
}

function fieldLabel(fields, arrow) {
  if (!fields.length) return null;
  const shown = fields.slice(0, 3).join(', ');
  const more = fields.length > 3 ? ` +${fields.length - 3}` : '';
  return arrow === 'in' ? `${shown}${more} →` : `→ ${shown}${more}`;
}

/** 'in' | 'out' | 'done' | 'idle' | null (not running: no decoration). */
export function edgeFlow(edge, from, to, active) {
  if (!active) return null;
  if (to === 'running' && (DONE.has(from) || from === 'running')) return 'in';
  if (from === 'running') return 'out';
  if (DONE.has(from) && DONE.has(to)) return 'done';
  return 'idle';
}

/**
 * Edges decorated for a live run. Nothing here is persisted: the canvas
 * saves `edges`, never these.
 */
export function flowEdges(edges, statusOf, active) {
  return edges.map((edge) => {
    const control = edge.data?.control_only;
    const flow = edgeFlow(edge, statusOf(edge.source), statusOf(edge.target), active);
    if (!flow) return edge;
    const fields = control ? [] : carriedFields(edge);
    const className = [edge.className, `edge-run-${flow}`, control ? 'edge-run-control' : '']
      .filter(Boolean).join(' ');
    const label = flow === 'in' ? fieldLabel(fields, 'in')
      : flow === 'out' ? fieldLabel(fields, 'out')
        : flow === 'done' ? null : edge.label;
    return {
      ...edge,
      animated: flow === 'in' && !control,
      className,
      label: label ?? (flow === 'idle' ? edge.label : undefined),
      // React Flow styles edge labels inline, not by class.
      labelStyle: flow === 'in' ? LABEL_IN : flow === 'out' ? LABEL_OUT : edge.labelStyle,
      labelBgStyle: flow === 'in' || flow === 'out' ? LABEL_BG : edge.labelBgStyle,
      zIndex: flow === 'in' || flow === 'out' ? 5 : edge.zIndex,
    };
  });
}

/**
 * How each node takes part in the run: the running ones are the focus,
 * everything else steps back while anything runs.
 */
export function nodeFocus(runStatus, anyRunning) {
  if (!anyRunning) return null;
  return runStatus === 'running' ? 'active' : 'dim';
}

/** "in: news, as_of · out: decision" for the node that is working now. */
export function ioSummary(data) {
  const names = (list) => (list || []).map((p) => (typeof p === 'string' ? p : p?.name)).filter(Boolean);
  const inputs = names(data?.inputs);
  const outputs = names(data?.outputs);
  if (!inputs.length && !outputs.length) return null;
  const short = (list) => (list.length > 3 ? `${list.slice(0, 3).join(', ')} +${list.length - 3}` : list.join(', '));
  return { inputs: short(inputs), outputs: short(outputs) };
}
