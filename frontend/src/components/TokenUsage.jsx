import React from 'react';

const count = (value) => Number(value || 0).toLocaleString();

// While a run is in progress the totals are what has been reported so far;
// they grow as each model call returns. Nothing reported is never shown as 0.
export default function TokenUsage({ usage, running = false }) {
  if (usage?.reported_calls) {
    return <p className="muted small" data-testid="token-usage" data-live={running ? 'true' : undefined}>
      {running
        ? `Reported tokens so far: ${count(usage.total_tokens)} · input ${count(usage.input_tokens)} / output ${count(usage.output_tokens)}. Updating as each model call returns; includes only calls that reported usage.`
        : `Reported tokens: ${count(usage.total_tokens)} · input ${count(usage.input_tokens)} / output ${count(usage.output_tokens)}. Includes only calls that reported usage.`}
    </p>;
  }
  return <p className="muted small" data-testid="token-usage">{running
    ? 'Token usage: no provider usage reported yet.'
    : 'Token usage unavailable: no provider usage has been reported.'}</p>;
}
