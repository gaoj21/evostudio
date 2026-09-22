import React from 'react';

export default function ChatActivity({ activity = [] }) {
  if (!activity.length) return null;
  return <details className="chat-activity"><summary>Calculations & sources ({activity.length})</summary>
    {activity.map((entry, index) => entry.operation?.op === 'compute' ? <div className="result-computation" key={index}>
      <strong>Python calculation · {entry.result?.status || 'error'}</strong>
      <details><summary>Calculation code</summary><pre>{entry.operation.code}</pre></details>
      <pre>{entry.result?.stdout || entry.result?.error || 'No printed output'}</pre>
      {entry.result?.stderr && <pre>{entry.result.stderr}</pre>}
    </div> : <pre key={index}>{JSON.stringify(entry, null, 2)}</pre>)}
  </details>;
}
