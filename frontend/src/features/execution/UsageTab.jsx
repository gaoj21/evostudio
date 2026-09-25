import React, { useEffect, useMemo, useState } from 'react';
import { api } from '../../api.js';

// Where a run's or a batch's tokens went: the total and what it cost, which
// node spent it, and — for a batch — which record. Every figure is what the
// provider reported; "not reported" is said, never shown as 0. Costs come
// from the machine's own llm package pricing, so the page never guesses one.

const count = (value) => Number(value || 0).toLocaleString();
const money = (cost) => (cost?.total_cost == null ? '—'
  : `$${cost.total_cost < 0.01 ? cost.total_cost.toFixed(5) : cost.total_cost.toFixed(4)}`);
const percent = (share) => (share == null ? '' : `${(share * 100).toFixed(share < 0.1 ? 1 : 0)}%`);
const RECORDS_SHOWN = 50;

// What the page showing a run or batch polls anyway: when any of it changes,
// the Usage tab fetches again, so it moves with the status bar.
export function usageVersion(target) {
  if (!target) return '';
  const usage = target.token_usage || {};
  const stage = target.stage || {};
  const counts = target.counts ? JSON.stringify(target.counts) : '';
  return [target.status, usage.total_tokens, usage.reported_calls, stage.node, stage.done, counts].join('|');
}

function Summary({ total, running }) {
  if (!total?.reported) {
    return <p className="muted small" data-testid="usage-none">{running
      ? 'No provider usage reported yet. Figures appear as each model call returns.'
      : 'No provider usage was reported for this one.'}</p>;
  }
  const cached = total.cache_read_tokens;
  const reasoning = total.reasoning_tokens;
  const cells = [
    ['Total tokens', count(total.total_tokens)],
    ['Input', `${count(total.input_tokens)}${cached != null ? ` · cached ${count(cached)}` : ''}`],
    ['Output', `${count(total.output_tokens)}${reasoning != null ? ` · reasoning ${count(reasoning)}` : ''}`],
    ['Model calls', count(total.reported_calls)],
    ['Cost', money(total.cost)],
  ];
  return <>
    <div className="usage-summary" data-testid="usage-summary">
      {cells.map(([label, value]) => (
        <div key={label} className="usage-cell"><small>{label}</small><strong>{value}</strong></div>
      ))}
    </div>
    <p className="muted small">
      {running ? 'So far — updating while it runs. ' : ''}
      {total.cost?.input_price_per_1m != null
        ? `Priced by the llm package: input ${total.cost.input_price_per_1m}/1M, output ${total.cost.output_price_per_1m}/1M${total.cost.cached_input_ratio != null ? `, cached input ×${total.cost.cached_input_ratio}` : ''}.`
        : 'Cost is shown when the llm package can price it.'}
    </p>
  </>;
}

function Bar({ share }) {
  return <span className="usage-bar" aria-hidden="true">
    <span style={{ width: `${Math.min(100, (share || 0) * 100)}%` }} />
  </span>;
}

function Row({ label, status, usage, share }) {
  return <tr>
    <td>{label}{status && <span className="muted small"> · {status}</span>}</td>
    {usage.reported ? <>
      <td>{count(usage.input_tokens)}</td>
      <td>{count(usage.output_tokens)}</td>
      <td>{count(usage.total_tokens)}</td>
      <td>{count(usage.reported_calls)}</td>
      <td>{money(usage.cost)}</td>
      <td className="usage-share"><Bar share={share}/>{percent(share)}</td>
    </> : <td colSpan={6} className="muted small">not reported</td>}
  </tr>;
}

function Table({ title, rows, labelOf, testid }) {
  return <section className="usage-section">
    <h4>{title}</h4>
    <table className="usage-table" data-testid={testid}>
      <thead><tr><th/><th>Input</th><th>Output</th><th>Total</th><th>Calls</th><th>Cost</th><th>Share</th></tr></thead>
      <tbody>{rows.map((row) => <Row key={labelOf(row)} label={labelOf(row)} status={row.status}
                                      usage={row.usage} share={row.share}/>)}</tbody>
    </table>
  </section>;
}

export default function UsageTab({ runId, batchId, live = false, version = '' }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [byTokens, setByTokens] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const [updatedAt, setUpdatedAt] = useState(null);

  // Fetched again whenever the run or batch the page is showing moves on
  // (`version`: its status, tokens and stage), and every two seconds while it
  // runs. A request that fails is retried, not the end of the updates.
  useEffect(() => {
    if (!runId && !batchId) return undefined;
    let active = true;
    let timer = null;
    const load = async () => {
      let running = true;
      try {
        const next = await api.getUsage(batchId ? { batchId } : { runId });
        if (!active) return;
        setData(next); setError(''); setUpdatedAt(new Date());
        running = next.running;
      } catch (err) {
        if (!active) return;
        setError(err?.body?.detail || err.message);
      }
      if (active && running) timer = setTimeout(load, 2000);
    };
    load();
    return () => { active = false; clearTimeout(timer); };
  }, [runId, batchId, live, version]);

  const records = useMemo(() => {
    const rows = [...(data?.by_record || [])];
    if (byTokens) rows.sort((a, b) => (b.usage.total_tokens || -1) - (a.usage.total_tokens || -1));
    return rows;
  }, [data, byTokens]);

  if (!runId && !batchId) return <div className="drawer-body"><p className="muted small">Nothing has run yet.</p></div>;
  if (error && !data) return <div className="drawer-body"><p role="alert" className="chat-error">{error}</p></div>;
  if (!data) return <div className="drawer-body"><p className="muted small">Loading usage…</p></div>;

  const shown = showAll ? records : records.slice(0, RECORDS_SHOWN);
  return <div className="drawer-body usage-tab">
    <p className="muted small" data-testid="usage-updated">
      {data.running ? 'Live · ' : ''}updated {updatedAt ? updatedAt.toLocaleTimeString() : '—'}
      {error ? ` · last refresh failed: ${error}` : ''}
    </p>
    <Summary total={data.total} running={data.running}/>
    {(data.by_node || []).length > 0 && (
      <Table title="By node" rows={data.by_node} labelOf={(row) => row.node} testid="usage-by-node"/>
    )}
    {data.by_record && (
      <section className="usage-section">
        <div className="usage-records-head">
          <h4>By record</h4>
          <label className="muted small">
            <input type="checkbox" checked={byTokens} onChange={(e) => setByTokens(e.target.checked)}/> most tokens first
          </label>
        </div>
        <table className="usage-table" data-testid="usage-by-record">
          <thead><tr><th/><th>Input</th><th>Output</th><th>Total</th><th>Calls</th><th>Cost</th><th>Share</th></tr></thead>
          <tbody>{shown.map((row) => <Row key={row.index} label={`#${row.index + 1}`} status={row.status}
                                          usage={row.usage} share={row.share}/>)}</tbody>
        </table>
        {records.length > RECORDS_SHOWN && (
          <button type="button" className="link small" onClick={() => setShowAll((v) => !v)}>
            {showAll ? `Show the first ${RECORDS_SHOWN}` : `Show all ${records.length} records`}
          </button>
        )}
      </section>
    )}
  </div>;
}
