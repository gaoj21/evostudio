import React from 'react';
export default function TokenUsage({usage}) {
  return <p className="muted small">{usage?.reported_calls
    ? `Reported tokens: ${usage.total_tokens.toLocaleString()} · input ${usage.input_tokens.toLocaleString()} / output ${usage.output_tokens.toLocaleString()}. Includes only calls that reported usage.`
    : 'Token usage unavailable: no provider usage has been reported.'}</p>;
}
