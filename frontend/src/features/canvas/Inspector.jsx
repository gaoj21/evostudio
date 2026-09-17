import React, { useEffect, useState } from 'react';
import JsonView from '../../components/JsonView.jsx';
import { api } from '../../api.js';
import MemorySettings from '../memory/MemorySettings.jsx';
import DataLoaderInput from '../data/DataLoaderInput.jsx';
import EvaluatorInspector from '../evaluation/EvaluatorInspector.jsx';
import DatasetInput from '../data/DatasetInput.jsx';

const PARSE_MODES = ['str', 'json', 'title', 'xml'];
const PARAM_TYPES = ['str', 'int', 'float', 'bool', 'list', 'dict'];

function EnabledToggle({ node, onUpdate }) {
  const on = node.data.enabled !== false;
  return (
    <div className="field">
      <label className="param-req" title="A disabled node is not part of any run, whatever it is wired to">
        <input
          type="checkbox"
          checked={on}
          onChange={(e) => onUpdate(node.id, { enabled: e.target.checked })}
        />
        Enabled
      </label>
      {!on && (
        <div className="muted small">Off: skipped by every run and not offered as a start point.</div>
      )}
    </div>
  );
}

function SaveOutputToggle({ node, onUpdate }) {
  const checked = node.data.save_output !== false;
  return (
    <div className="field">
      <label className="param-req" title="Persist this node's output into the run's workspace artifacts (output.json)">
        <input
          type="checkbox"
          checked={checked}
          onChange={(e) => onUpdate(node.id, { save_output: e.target.checked })}
        />
        Save output to workspace
      </label>
      <div className="muted small">When off, this node's output is omitted from the run's output.json (it still flows to downstream nodes).</div>
    </div>
  );
}

function SourceInspector({ node, onUpdate, onRename, getGraph }) {
  const [schemas, setSchemas] = useState(null);

  useEffect(() => {
    api
      .listSourceTypes()
      .then((r) => {
        const m = {};
        (r.source_types || []).forEach((s) => {
          m[s.type] = s;
        });
        setSchemas(m);
      })
      .catch(() => setSchemas({}));
  }, []);

  const d = node.data;
  const cfg = d.source || { type: 'credit_risk' };
  const schema = schemas?.[cfg.type];
  const setCfg = (patch) => onUpdate(node.id, { source: { ...cfg, ...patch } });
  const [probing, setProbing] = useState(false);
  const [probeNote, setProbeNote] = useState(null);
  const probe = async () => {
    setProbing(true);
    setProbeNote(null);
    try {
      const found = await api.probeSource(cfg);
      onUpdate(node.id, { outputs: found.fields.map((name) => ({
        name, type: 'str', description: `${cfg.type} output`, required: false })) });
      setProbeNote(`${found.records} record(s); fields: ${found.fields.join(', ')}`);
    } catch (err) {
      setProbeNote(`Could not run it: ${err?.body?.detail || err.message}`);
    } finally {
      setProbing(false);
    }
  };

  return (
    <aside className="inspector">
      <h3>Input: {node.id}</h3>
      <div className="field">
        <label>Name</label>
        <input value={d.editName ?? node.id} onChange={(e) => onUpdate(node.id, { editName: e.target.value })} onBlur={() => onRename(node.id, d.editName ?? node.id)} />
      </div>
      {cfg.type === 'dataloader' && <DataLoaderInput key={node.id} config={cfg} nodeId={node.id} getGraph={getGraph} onChange={(source,outputs) => onUpdate(node.id,{source,...(outputs?{outputs}:{})})} />}
      {cfg.type === 'user_dataset' && <DatasetInput key={node.id} config={cfg}
        onChange={(source, outputs) => onUpdate(node.id, { source, ...(outputs ? { outputs } : {}) })} />}
      {cfg.type !== 'dataloader' && <button onClick={() => onUpdate(node.id,{source:{type:'dataloader',loader:'source',source_config:cfg,n:0,read_batch_size:100,...(cfg.type==='credit_risk' && cfg.step && cfg.step!=='none'?{group_by:'sample_id',order_by:'as_of'}:{})}})}>Use DataLoader preprocessing</button>}
      {!schema && <p className="muted small">Loading source schema…</p>}
      {(['user_dataset','dataloader'].includes(cfg.type) ? [] : schema?.config || []).map((f) => (
        <div className="field" key={f.name}>
          <label htmlFor={`source-config-${f.name}`}>
            {f.label || f.name}
            {f.required ? ' *' : ''}
          </label>
          {f.type === 'select' ? (
            <select id={`source-config-${f.name}`} value={cfg[f.name] ?? (f.name === 'step' && cfg.dataset && cfg.dataset !== 'contemporary' ? 'daily' : f.default) ?? ''} onChange={(e) => setCfg({ [f.name]: e.target.value, ...(f.name === 'dataset' ? { split: 'dev' } : {}) })}>
              {(f.name === 'split' && cfg.dataset && cfg.dataset !== 'contemporary' ? ['', 'dev', 'test'] : f.options || []).map((o) => (
                <option key={o} value={o}>
                  {o === '' ? '(all)' : o}
                </option>
              ))}
            </select>
          ) : f.type === 'number' ? (
            <input type="number" value={cfg[f.name] ?? f.default ?? 0} onChange={(e) => setCfg({ [f.name]: Number(e.target.value) })} />
          ) : f.type === 'textarea' ? (
            <textarea rows={3} value={cfg[f.name] ?? f.default ?? ''} onChange={(e) => setCfg({ [f.name]: e.target.value })} />
          ) : (
            <input value={cfg[f.name] ?? f.default ?? ''} onChange={(e) => setCfg({ [f.name]: e.target.value })} />
          )}
        </div>
      ))}
      {cfg.type === 'credit_risk' && cfg.dataset && cfg.dataset !== 'contemporary' && <p className="muted small">Samples selects trajectories. Walk each window chooses daily, weekly or monthly batches of new evidence; none runs the whole window once. Outcomes stay outside the inputs.</p>}
      <details className="input-disclosure" open={cfg.type !== 'user_dataset'}><summary>Scheduling & node settings</summary>
      <div className="field">
        <label>Schedule</label>
        {(() => {
          const schedule = cfg.schedule || { mode: 'ondemand' };
          const setSchedule = (patch) => setCfg({ schedule: { ...schedule, ...patch } });
          return (
            <>
              <select value={schedule.mode || 'ondemand'} onChange={(e) => setSchedule({ mode: e.target.value })}>
                <option value="ondemand">On demand (manual runs)</option>
                <option value="daily">Daily at a fixed time</option>
                <option value="interval">Every N minutes</option>
              </select>
              {schedule.mode === 'daily' && (
                <input
                  type="time"
                  value={schedule.time || '09:00'}
                  onChange={(e) => setSchedule({ time: e.target.value })}
                  style={{ marginTop: 6 }}
                />
              )}
              {schedule.mode === 'interval' && (
                <input
                  type="number"
                  min="5"
                  value={schedule.interval_minutes ?? 60}
                  onChange={(e) => setSchedule({ interval_minutes: Number(e.target.value) || 60 })}
                  style={{ marginTop: 6 }}
                />
              )}
              {schedule.mode !== 'ondemand' && (
                <div className="muted small" style={{ marginTop: 4 }}>
                  New data fires a run automatically; start watching from the top bar.
                </div>
              )}
            </>
          );
        })()}
      </div>
      {schema?.custom ? (
        <>
          {/* A custom source's outputs are whatever its tool returns; the
              node declares them, or asks the tool once. */}
          <ParamList label="Outputs" items={d.outputs || []} onChange={(outputs) => onUpdate(node.id, { outputs })} />
          <div className="field">
            <button type="button" disabled={probing} onClick={probe}>
              {probing ? 'Running…' : 'Detect outputs by running it once'}
            </button>
            {probeNote && <div className="muted small">{probeNote}</div>}
          </div>
        </>
      ) : (
        <div className="field">
          <label>Outputs</label>
          <div className="muted small">{(d.outputs || []).map((o) => o.name).join(', ')}</div>
        </div>
      )}
      <EnabledToggle node={node} onUpdate={onUpdate} />
        <SaveOutputToggle node={node} onUpdate={onUpdate} />
      </details>
    </aside>
  );
}

function ToolsSelect({ selected, onChange }) {
  const [catalog, setCatalog] = useState([]);

  useEffect(() => {
    api.listTools().then((r) => setCatalog(r.tools || [])).catch(() => setCatalog([]));
  }, []);

  const toggle = (name) => {
    onChange(
      selected.includes(name)
        ? selected.filter((n) => n !== name)
        : [...selected, name]
    );
  };

  if (catalog.length === 0) return null;
  return (
    <div className="field">
      <label>Tools</label>
      <div className="muted small" style={{ marginBottom: 4 }}>
        Tick to attach; manage custom tools in the left "Custom" tab.
      </div>
      {catalog.map((t) => (
        <label
          key={t.name}
          className={`tool-option${t.available ? '' : ' unavailable'}`}
          title={t.available ? t.description : `${t.description} — ${t.unavailable_reason}`}
        >
          <input
            type="checkbox"
            disabled={!t.available}
            checked={selected.includes(t.name)}
            onChange={() => toggle(t.name)}
          />
          <span className="tool-option-name">{t.name}</span>
          {!t.available && <span className="muted small">requires {t.requires.join(', ')}</span>}
        </label>
      ))}
    </div>
  );
}

function SkillsSelect({ selected, onChange }) {
  const [catalog, setCatalog] = useState([]);

  useEffect(() => {
    api.listSkills().then((r) => setCatalog(r.skills || [])).catch(() => setCatalog([]));
  }, []);

  const toggle = (name) => {
    onChange(
      selected.includes(name) ? selected.filter((n) => n !== name) : [...selected, name]
    );
  };

  return (
    <div className="field">
      <label>Skills</label>
      <div className="muted small" style={{ marginBottom: 4 }}>
        Instructions this node always follows — appended to its system prompt at
        run time. Manage them in the left &quot;Custom&quot; tab, or ask in Chat.
      </div>
      {catalog.length === 0 ? (
        <div className="muted small">No skills yet.</div>
      ) : (
        catalog.map((sk) => (
          <label key={sk.name} className="tool-option" title={sk.description}>
            <input
              type="checkbox"
              checked={selected.includes(sk.name)}
              onChange={() => toggle(sk.name)}
            />
            <span className="tool-option-name">{sk.name}</span>
          </label>
        ))
      )}
    </div>
  );
}

// The framework requires every declared input to appear as a {placeholder} in
// the prompt and raises a KeyError at run time otherwise — deep in a traceback,
// after the user has already paid for the run. Catch it while editing instead.
function UnusedInputs({ inputs, prompt }) {
  const text = String(prompt || '');
  const unused = (inputs || [])
    .map((i) => i.name)
    .filter((n) => n && !text.includes(`{${n}}`));
  if (unused.length === 0) return null;
  return (
    <div className="muted small unused-note">
      Not referenced in the prompt — the run will fail until{' '}
      {unused.length > 1 ? 'these are' : 'this is'} used as{' '}
      <span className="tool-option-name">{unused.map((n) => `{${n}}`).join(', ')}</span>{' '}
      or removed from Inputs.
    </div>
  );
}

// Data flows by name, so an input no node produces is filled in at run time.
// Saying so where the inputs are edited turns a run-time surprise (or a typo)
// into something visible while you are writing it.
function UnwiredInputs({ inputs, producedNames }) {
  if (!producedNames) return null;
  const unwired = (inputs || []).map((i) => i.name).filter((n) => n && !producedNames.has(n));
  if (unwired.length === 0) return null;
  return (
    <div className="muted small unwired-note">
      Asked for at run time (no node produces {unwired.length > 1 ? 'these' : 'this'}):{' '}
      <span className="tool-option-name">{unwired.join(', ')}</span>
    </div>
  );
}

function ParamList({ label, items, onChange }) {
  const update = (i, patch) => {
    const next = items.map((it, idx) => (idx === i ? { ...it, ...patch } : it));
    onChange(next);
  };
  const remove = (i) => onChange(items.filter((_, idx) => idx !== i));
  const add = () =>
    onChange([...items, { name: '', type: 'str', description: '', required: true }]);
  return (
    <div className="param-list">
      <div className="param-list-head">
        <span>{label}</span>
        <button type="button" onClick={add}>
          + Add
        </button>
      </div>
      {items.map((p, i) => (
        <div className="param-row" key={i}>
          <input
            className="param-name"
            placeholder="name"
            value={p.name}
            onChange={(e) => update(i, { name: e.target.value })}
          />
          <select value={p.type || 'str'} onChange={(e) => update(i, { type: e.target.value })}>
            {PARAM_TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
          <input
            className="param-desc"
            placeholder="description"
            value={p.description || ''}
            onChange={(e) => update(i, { description: e.target.value })}
          />
          <label className="param-req" title="required">
            <input
              type="checkbox"
              checked={!!p.required}
              onChange={(e) => update(i, { required: e.target.checked })}
            />
            req
          </label>
          <button type="button" className="param-del" onClick={() => remove(i)}>
            ✕
          </button>
        </div>
      ))}
    </div>
  );
}

// A preprocessor is an ordinary custom tool taking one object (the record) and
// returning one: it runs over every input before the workflow sees it, for a
// batch and a single run alike.
function PreprocessSelect({ value, disabled, onChange }) {
  const [tools, setTools] = useState([]);

  useEffect(() => {
    api
      .listCustomTools()
      .then((r) => setTools(r.tools || []))
      .catch(() => setTools([]));
  }, []);

  const usable = tools.filter((t) => (t.params || []).length === 1);
  const chosen = tools.find((t) => t.name === value);

  return (
    <div className="field">
      <label>Preprocess inputs</label>
      <select value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)}>
        <option value="">None — run the inputs as they arrive</option>
        {usable.map((t) => (
          <option key={t.name} value={t.name}>
            {t.name}
          </option>
        ))}
        {value && !chosen && <option value={value}>{value} (missing)</option>}
      </select>
      <div className="muted small">
        {chosen
          ? chosen.description
          : 'A custom tool that takes one record and returns the cleaned one. '
            + 'Applies to every record of a batch and to a single run.'}
      </div>
      {value && !chosen && (
        <div className="chat-error">
          No custom tool named {value} — runs will refuse to start until it exists
          or this is cleared.
        </div>
      )}
    </div>
  );
}

// With nothing selected the Inspector shows the graph's own settings: they are
// the properties of what you are looking at, and they no longer need a
// permanent row in the top bar.
function GraphSettings({ graph, onGraphChange, runMode }) {
  return (
    <aside className="inspector">
      <h3>Workflow settings</h3>
      {!graph ? (
        <p className="muted">No workflow open.</p>
      ) : (
        <>
          <p className="workflow-settings-name">
            <strong>{graph.name}</strong>
            <span className="muted small">Rename from the workflow picker above.</span>
          </p>
          <div className="field">
            <label>Goal</label>
            <textarea
              rows={3}
              placeholder="What this workflow achieves"
              value={graph.goal || ''}
              disabled={runMode}
              onChange={(e) => onGraphChange({ goal: e.target.value })}
            />
            <div className="muted small">
              Passed to the framework when the workflow runs. Chat can write it for you.
            </div>
          </div>
          <PreprocessSelect
            value={graph.preprocess || ''}
            disabled={runMode}
            onChange={(preprocess) => onGraphChange({ preprocess: preprocess || null })}
          />
          <div className="field">
            <label>Output directory</label>
            <input
              placeholder="runs"
              value={graph.output_dir || ''}
              disabled={runMode}
              onChange={(e) => onGraphChange({ output_dir: e.target.value })}
            />
            <div className="muted small">Workspace folder for run artifacts.</div>
          </div>
        </>
      )}
      <p className="muted small">Select a node to edit it.</p>
    </aside>
  );
}

export default function Inspector({ node, runInfo, runMode, onUpdate, onRename, graph, getGraph, onGraphChange, producedNames, siblings }) {
  if (!node) {
    return <GraphSettings graph={graph} onGraphChange={onGraphChange} runMode={runMode} />;
  }

  if (runMode) {
    return (
      <aside className="inspector">
        <h3>Run: {node.id}</h3>
        <div className="field">
          <label>Status</label>
          <div className={`node-status status-${runInfo?.status || 'pending'}`}>
            {runInfo?.status || 'pending'}
          </div>
        </div>
        {runInfo?.status === 'completed' && runInfo.output != null && (
          <div className="field">
            <label>Output</label>
            <JsonView value={runInfo.output} />
          </div>
        )}
      </aside>
    );
  }

  const d = node.data;

  if (d.kind === 'evaluator') return <EvaluatorInspector node={node} onUpdate={onUpdate} onRename={onRename} getGraph={getGraph} />;
  if (d.kind === 'source') {
    return <SourceInspector node={node} onUpdate={onUpdate} onRename={onRename} getGraph={getGraph} />;
  }

  if (d.kind === 'tool') {
    return (
      <aside className="inspector">
        <h3>Tool: {node.id}</h3>
        <div className="field">
          <label>Name</label>
          <input value={d.editName ?? node.id} onChange={(e) => onUpdate(node.id, { editName: e.target.value })} onBlur={() => onRename(node.id, d.editName ?? node.id)} />
        </div>
        <div className="field">
          <label>Bound tool</label>
          <div className="muted">⚙ {d.tool || '(none)'}</div>
          <div className="muted small">Deterministic execution, no LLM. Runs in connection order; inputs can come from upstream nodes or workflow inputs.</div>
        </div>
        <ParamList label="Inputs (tool params)" items={d.inputs || []} onChange={(inputs) => onUpdate(node.id, { inputs })} />
        <ParamList label="Outputs" items={d.outputs || []} onChange={(outputs) => onUpdate(node.id, { outputs })} />
        <EnabledToggle node={node} onUpdate={onUpdate} />
        <SaveOutputToggle node={node} onUpdate={onUpdate} />
      </aside>
    );
  }

  return (
    <aside className="inspector">
      <h3>Node: {node.id}</h3>
      <div className="field">
        <label>Name</label>
        <input value={d.editName ?? node.id} onChange={(e) => onUpdate(node.id, { editName: e.target.value })} onBlur={() => onRename(node.id, d.editName ?? node.id)} />
      </div>
      <div className="field">
        <label>Description</label>
        <textarea rows={2} value={d.description} onChange={(e) => onUpdate(node.id, { description: e.target.value })} />
      </div>
      <div className="field">
        <label>Prompt</label>
        <textarea rows={5} value={d.prompt} onChange={(e) => onUpdate(node.id, { prompt: e.target.value })} />
      </div>
      <div className="field">
        <label>System prompt</label>
        <textarea rows={3} value={d.system_prompt} onChange={(e) => onUpdate(node.id, { system_prompt: e.target.value })} />
      </div>
      <div className="field">
        <label>Parse mode</label>
        <select value={d.parse_mode} onChange={(e) => onUpdate(node.id, { parse_mode: e.target.value })}>
          {PARSE_MODES.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
      </div>
      <ParamList label="Inputs" items={d.inputs} onChange={(inputs) => onUpdate(node.id, { inputs })} />
      <UnusedInputs inputs={d.inputs} prompt={d.prompt} />
      <UnwiredInputs inputs={d.inputs} producedNames={producedNames} />
      <ParamList label="Outputs" items={d.outputs} onChange={(outputs) => onUpdate(node.id, { outputs })} />
      <fieldset className="field">
        <legend>Harness</legend>
        <label>Execution engine<select value={d.harness?.engine || 'workflow'} onChange={e => onUpdate(node.id, { harness: e.target.value === 'workflow' ? null : { engine: 'deepagents', max_steps: 20, timeout: 300 } })}>
          <option value="workflow">Standard workflow Agent</option><option value="deepagents">Deep Agents + LangGraph</option>
        </select></label>
        {d.harness?.engine === 'deepagents' && <>
          <label>Maximum model steps<input type="number" min="1" max="100" value={d.harness.max_steps || 20} onChange={e => onUpdate(node.id, { harness: { ...d.harness, max_steps: Number(e.target.value) } })} /></label>
          <label>Time limit (seconds)<input type="number" min="10" max="1800" value={d.harness.timeout || 300} onChange={e => onUpdate(node.id, { harness: { ...d.harness, timeout: Number(e.target.value) } })} /></label>
          <p className="muted small">Uses this node’s selected tools, Skills and Memory. Each workflow execution has isolated conversation state.</p>
        </>}
      </fieldset>
      {/* After the parameter lists: what memory keeps is chosen from them. */}
      <MemorySettings graphId={graph?.id} node={node} onUpdate={onUpdate} siblings={siblings} />
      <ToolsSelect key={node.id} selected={d.tool_names || []} onChange={(tool_names) => onUpdate(node.id, { tool_names })} />
      <SkillsSelect key={`skills-${node.id}`} selected={d.skill_names || []} onChange={(skill_names) => onUpdate(node.id, { skill_names })} />
      <EnabledToggle node={node} onUpdate={onUpdate} />
        <SaveOutputToggle node={node} onUpdate={onUpdate} />
    </aside>
  );
}
