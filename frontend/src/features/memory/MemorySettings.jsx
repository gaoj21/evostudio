import React, { useState } from 'react';
import { Mem0SpacePicker } from './Mem0Spaces.jsx';

export const DEFAULT_RETRIEVE = 3;
export const DEFAULT_SESSION_RECALL = 5;
export const MAX_RETRIEVE = 10;

// `null` means "keep all of them", which is what every node did before this
// setting existed, and stays the default. An empty array is a real choice:
// keep the conclusions without the material they came from.
const ALL = null;

/**
 * What the memory panel needs to know about the rest of the workflow.
 *
 * Every node, not only the ones that reason: a node choosing which of the
 * run's fields to record needs the source node's outputs too — the observation
 * window comes from there. Only nodes that keep a memory can be read *from*,
 * which `remembers` marks.
 */
export function memorySiblings(nodes) {
  return (nodes || []).map((n) => ({
    name: n.id,
    remembers: (!n.data?.kind || n.data.kind === 'task')
      && !!n.data?.use_long_term_memory,
    fields: [...(n.data?.inputs || []), ...(n.data?.outputs || [])]
      .map((f) => f.name).filter(Boolean),
  }));
}

export function isKept(selected, name) {
  return selected === ALL || selected.includes(name);
}

/**
 * Ticking a field off a list that means "all" has to turn it into a real list
 * first, otherwise the first click would silently do nothing.
 */
export function toggleField(selected, name, declared) {
  const current = selected === ALL ? declared.slice() : selected.slice();
  const next = current.includes(name)
    ? current.filter((f) => f !== name)
    : [...declared.filter((f) => current.includes(f) || f === name)];
  // Back to every declared field: store "all" rather than a list that would
  // silently stop covering a field added later.
  return next.length === declared.length ? ALL : next;
}

/**
 * Which stores a node reads, as a real list once it stops being just its own.
 *
 * `null` here means "its own store", not "every node's" — a node reading
 * everything by default would be a surprising thing to do with someone's
 * history, so narrowing starts from itself.
 */
export function toggleReadFrom(selected, name, own) {
  const current = selected === ALL ? [own] : selected.slice();
  const next = current.includes(name)
    ? current.filter((n) => n !== name)
    : [...current, name];
  return (next.length === 1 && next[0] === own) ? ALL : next;
}

export function summarise(policy, outputs, inputs) {
  const part = (label, selected, declared) => {
    if (selected === ALL) return `all ${label}`;
    if (!selected.length) return null;
    if (selected.length === declared.length) return `all ${label}`;
    return `${selected.length} of ${declared.length} ${label}`;
  };
  const kept = [part('outputs', policy.outputs, outputs),
                part('inputs', policy.inputs, inputs)].filter(Boolean);
  if (!kept.length) return 'nothing selected';
  // Which of the two it is matters more than what is in it: one is an exact
  // record, the other a similarity search, and they fail in different ways.
  const where = (policy.kind || (policy.match ? 'table' : 'recall')) === 'table' ? `a table per ${policy.match}` : 'a searchable corpus';
  return `keeps ${kept.join(' + ')} in ${where}`;
}

// "Record detection.source": a key inside a node's JSON answer. Offered as a
// typed name rather than a list, since what keys an answer holds is the
// prompt's business, not the schema's.
function KeyInside({ nodeId, fields, onAdd }) {
  const [text, setText] = useState('');
  const head = text.split('.')[0];
  const valid = text.includes('.') && fields.includes(head) && !text.endsWith('.');
  const submit = () => { if (valid) { onAdd(text.trim()); setText(''); } };
  return (
    <div className="memory-row">
      <input
        id={`mem-key-${nodeId}`}
        aria-label="Record a key inside a field"
        placeholder="e.g. detection.source"
        value={text}
        onChange={(e) => setText(e.target.value.trim())}
        onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); submit(); } }}
      />
      <button type="button" disabled={!valid} onClick={submit}>Record key</button>
      {text && !valid && (
        <div className="muted small">
          {text.includes('.') ? `No field named ${head} in this run.` : 'field.key — e.g. detection.source'}
        </div>
      )}
    </div>
  );
}

function FieldList({ label, declared, selected, onToggle }) {
  if (!declared.length) {
    return (
      <div className="memory-group">
        <div className="memory-group-head">{label}</div>
        <div className="muted small">This node has no {label.toLowerCase()}.</div>
      </div>
    );
  }
  return (
    <div className="memory-group">
      <div className="memory-group-head">{label}</div>
      {declared.map((name) => (
        <label key={name} className="memory-field">
          <input
            type="checkbox"
            checked={isKept(selected, name)}
            onChange={() => onToggle(name)}
          />
          {name}
        </label>
      ))}
    </div>
  );
}

/**
 * What one node keeps in its long-term memory.
 *
 * Memory used to be a single switch that stored everything the node touched,
 * so a node reading a page of text remembered the page and not the judgement
 * it reached. The fields are listed here because the answer differs per node
 * and only the person building it knows which way round it should be.
 */
export default function MemorySettings({ node, onUpdate, siblings = [], graphId }) {
  const d = node.data;
  const enabled = !!d.use_long_term_memory;
  const raw = d.memory || {};
  const policy = {
    write_enabled: raw.write_enabled !== false,
    read_enabled: raw.read_enabled !== false,
    outputs: raw.outputs === undefined ? ALL : raw.outputs,
    inputs: raw.inputs === undefined ? ALL : raw.inputs,
    when: raw.when || 'success',
    retrieve: raw.retrieve ?? DEFAULT_RETRIEVE,
    session_recall: raw.session_recall ?? DEFAULT_SESSION_RECALL,
    match: raw.match || '',
    context: raw.context || [],
    at: raw.at || '',
    read_from: raw.read_from === undefined ? ALL : raw.read_from,
    read: raw.read === undefined ? ALL : raw.read,
  };
  const outputs = (d.outputs || []).map((p) => p.name).filter(Boolean);
  const inputs = (d.inputs || []).map((p) => p.name).filter(Boolean);

  // Written whole so a partial policy can never be persisted; `null` for a
  // list is meaningful, so it is kept rather than dropped.
  const patch = (fields) =>
    onUpdate(node.id, { memory: { ...raw, ...policy, ...fields } });

  const keepsNothing = policy.write_enabled && policy.outputs?.length === 0 && policy.inputs?.length === 0;

  // Which fields a recalled entry could carry: the fields of whichever nodes
  // this one reads.
  // Anything the workflow produces, whether or not this node consumes it —
  // the observation window usually belongs to the source node, not here.
  const runFields = [...new Set((siblings || []).flatMap((s) => s.fields))];
  // Only a node that keeps a memory has one to be read from.
  const remembering = (siblings || []).filter((s) => s.remembers !== false);
  const datable = [...new Set([...policy.context, ...inputs, ...outputs])];

  const readsFrom = policy.read_from === ALL ? [node.id] : policy.read_from;
  const readableFields = [...new Set(
    remembering
      .filter((s) => readsFrom.includes(s.name))
      .flatMap((s) => s.fields)
  )];
  const readsNothing = readsFrom.length === 0 && policy.retrieve > 0;

  return (
    <div className="field">
      <label
        className="param-req"
        title="Store this node's runs in a long-term memory store, and retrieve related past runs into its prompt"
      >
        <input
          type="checkbox"
          checked={enabled}
          onChange={(e) => onUpdate(node.id, { use_long_term_memory: e.target.checked })}
        />
        Long-term memory
      </label>

      {!enabled ? (
        <div className="muted small">
          Store this node&apos;s runs in long-term memory; later runs retrieve related
          memories into the prompt.
        </div>
      ) : (
        <div className="memory-settings">
          <label>Memory backend<select aria-label="Memory backend" value={raw.provider || 'legacy'}
            onChange={e => patch({ provider: e.target.value, ...(e.target.value === 'mem0' ? { kind: 'recall', match: '', at: '', read_from: null, read: null } : {}) })}>
            <option value="legacy">Existing node memory</option><option value="mem0">Mem0 shared space</option>
          </select></label>
          {raw.provider === 'mem0' && <>
            <Mem0SpacePicker key={graphId} graphId={graphId} value={raw.space_id || ''} onChange={space_id => patch({ space_id })} />
            <p className="muted small">Reads from the selected space, regardless of which Agent wrote it. Browse and edit entries in Memory → Mem0 shared memory. Writes preserve selected fields without model extraction.</p>
          </>}
          <label className="param-req"><input type="checkbox" checked={policy.read_enabled}
            onChange={(e) => patch({ read_enabled: e.target.checked })} />Read memory before execution</label>
          <label className="param-req"><input type="checkbox" checked={policy.write_enabled}
            onChange={(e) => patch({ write_enabled: e.target.checked })} />Write memory after the run</label>
          <div className="muted small">{policy.write_enabled ? summarise({ ...policy, kind: raw.kind }, outputs, inputs) : (policy.read_enabled ? 'Read-only: this node does not write memory.' : 'Reading and writing are both disabled.')}</div>
          <div className="muted small">Current-run results travel through workflow inputs. Memory is saved after the run finishes.</div>

          <FieldList
            label="Outputs"
            declared={outputs}
            selected={policy.outputs}
            onToggle={(name) => patch({ outputs: toggleField(policy.outputs, name, outputs) })}
          />
          <FieldList
            label="Inputs"
            declared={inputs}
            selected={policy.inputs}
            onToggle={(name) => patch({ inputs: toggleField(policy.inputs, name, inputs) })}
          />

          {keepsNothing && (
            <div className="chat-error">
              Nothing is selected, so this node would never write to its memory.
              Pick a field, or turn memory off.
            </div>
          )}
          {readsNothing && (
            <div className="chat-note">
              No store is selected, so this node writes memory without ever
              reading any back.
            </div>
          )}

          <div className="memory-group">
            <div className="memory-group-head">Also record</div>
            <div className="muted small">
              Fields from the run this node does not itself take — the period it
              covers, an id — so an entry says what it is about.
            </div>
            {runFields.filter((f) => !inputs.includes(f) && !outputs.includes(f))
              .map((name) => (
                <label key={name} className="memory-field">
                  <input
                    type="checkbox"
                    checked={policy.context.includes(name)}
                    onChange={() => patch({
                      context: policy.context.includes(name)
                        ? policy.context.filter((c) => c !== name)
                        : [...policy.context, name],
                      ...(policy.at === name ? { at: '' } : {}),
                    })}
                  />
                  {name}
                </label>
              ))}
            {/* A key inside a JSON answer — `detection.source` — is not a
                field of the run, so it cannot be ticked; it is typed. */}
            {policy.context.filter((c) => c.includes('.')).map((name) => (
              <label key={name} className="memory-field">
                <input
                  type="checkbox"
                  checked
                  onChange={() => patch({ context: policy.context.filter((c) => c !== name) })}
                />
                {name} <span className="muted small">(key inside {name.split('.')[0]})</span>
              </label>
            ))}
            <KeyInside
              nodeId={node.id}
              fields={runFields}
              onAdd={(name) => {
                if (!policy.context.includes(name)) patch({ context: [...policy.context, name] });
              }}
            />
          </div>

          {raw.provider !== 'mem0' && <>
          <div className="memory-row">
            <label htmlFor={`mem-at-${node.id}`}>Dated by</label>
            <select
              id={`mem-at-${node.id}`}
              value={policy.at}
              onChange={(e) => patch({ at: e.target.value })}
            >
              <option value="">When the run happened</option>
              {datable.map((name) => (
                <option key={name} value={name}>{name}</option>
              ))}
            </select>
          </div>
          {!policy.at && (
            <div className="muted small">
              Every record of one batch is written within the same minute, so
              that orders a timeline by nothing. Pick the field that says what
              period the entry covers.
            </div>
          )}

          <div className="memory-row">
            <label htmlFor={`mem-match-${node.id}`}>Keeps</label>
            <select
              id={`mem-match-${node.id}`}
              value={policy.match}
              onChange={(e) => patch({ match: e.target.value, kind: e.target.value ? 'table' : 'recall' })}
            >
              <option value="">Past runs, searched by similarity</option>
              {inputs.map((name) => (
                <option key={name} value={name}>{`A record per ${name}`}</option>
              ))}
            </select>
          </div>
          <div className="muted small">
            {policy.match
              ? (<>
                  A table keyed by <span className="tool-option-name">{policy.match}</span>:
                  one row per {policy.at
                    ? <><span className="tool-option-name">{policy.at}</span></>
                    : 'run'}, read back in time order. Exact, so a row is either
                  there or it is not.
                </>)
              : 'A searchable corpus of past runs. Answers "what resembles this", '
                + 'which is not the same as "what happened to this one before".'}
          </div>

          <div className="memory-group">
            <div className="memory-group-head">Reads from</div>
            {(remembering.length ? remembering : [{ name: node.id, fields: [] }]).map((sib) => (
              <label key={sib.name} className="memory-field">
                <input
                  type="checkbox"
                  checked={policy.read_from === ALL
                    ? sib.name === node.id
                    : policy.read_from.includes(sib.name)}
                  onChange={() => patch({
                    read_from: toggleReadFrom(policy.read_from, sib.name, node.id),
                    // The fields on offer change with the stores, so a
                    // narrowed selection is no longer meaningful.
                    read: ALL,
                  })}
                />
                {sib.name}
                {sib.name === node.id && <span className="muted small"> (its own)</span>}
              </label>
            ))}
          </div>

          {readableFields.length > 0 && (
            <div className="memory-group">
              <div className="memory-group-head">Reads back</div>
              {readableFields.map((field) => (
                <label key={field} className="memory-field">
                  <input
                    type="checkbox"
                    checked={isKept(policy.read, field)}
                    onChange={() => patch({
                      read: toggleField(policy.read, field, readableFields),
                    })}
                  />
                  {field}
                </label>
              ))}
            </div>
          )}

          </>}
          <div className="memory-row">
            <label>
              Session
              <input
                type="number"
                min={0}
                max={MAX_RETRIEVE}
                value={policy.session_recall}
                onChange={(e) => patch({ session_recall: Number(e.target.value) })}
              />
            </label>
            <span className="muted small">
              earlier steps of the same session, in order
              {policy.session_recall === 0 ? ' — none' : ''}
            </span>
          </div>

          <div className="memory-row"><label>Memory character budget
            <input type="number" min={200} max={20000} value={raw.limit ?? 4000}
              onChange={(e) => patch({ limit: Number(e.target.value) })} /></label></div>
          <div className="memory-row">
            <label>
              Recall
              <input
                type="number"
                min={0}
                max={MAX_RETRIEVE}
                value={policy.retrieve}
                onChange={(e) => patch({ retrieve: Number(e.target.value) })}
              />
            </label>
            <span className="muted small">
              {policy.match ? 'earlier records for this subject' : 'similar past runs'}
              {policy.retrieve === 0 ? ' — none' : ''}
            </span>
          </div>

          <label className="param-req">
            <input
              type="checkbox"
              checked={policy.when === 'always'}
              onChange={(e) => patch({ when: e.target.checked ? 'always' : 'success' })}
            />
            Remember failed runs too
          </label>
        </div>
      )}
    </div>
  );
}
