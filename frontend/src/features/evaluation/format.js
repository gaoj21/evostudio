export const STAGES = ['queued', 'baseline', 'optimizing', 'evaluating optimized'];

export function elapsed(task) {
  if (task.elapsed_seconds != null) return `${Math.round(task.elapsed_seconds / 60)} min`;
  if (!task.created_at) return '';
  const secs = (Date.now() - new Date(task.created_at).getTime()) / 1000;
  return `${Math.max(0, Math.round(secs / 60))} min so far`;
}

export const fmt = (x) => (x == null ? '—' : Number(x).toFixed(3));

