// Short forms of provider-reported token usage, for badges. Each returns an
// empty string when nothing was reported: no usage is never shown as zero.

const count = (value) => Number(value || 0).toLocaleString();

export function tokenSummary(usage, running = false) {
  if (!usage?.reported_calls) return '';
  return `${count(usage.total_tokens)} tokens${running ? ' so far' : ''}`;
}

export const tokenSuffix = (usage, running = false) => {
  const text = tokenSummary(usage, running);
  return text ? ` · ${text}` : '';
};

// "850 tok", "12.3k tok": small enough for a node badge.
export function compactTokens(usage) {
  if (!usage?.reported_calls) return '';
  const total = Number(usage.total_tokens || 0);
  const short = total >= 1e6 ? `${(total / 1e6).toFixed(1)}M` : total >= 1e3 ? `${(total / 1e3).toFixed(1)}k` : String(total);
  return `${short} tok`;
}
