import React, { useEffect, useState } from 'react';
import ResourceActions from '../../components/ResourceActions.jsx';
import { api } from '../../api.js';

// A tool is a documented calling interface, so a custom toolkit is a Python
// module: every public function in it is one tool, and everything the model is
// told about it — names, descriptions, parameters, types — is read back out of
// the code. Nothing is declared twice.
const CODE_PLACEHOLDER = `"""Text statistics."""


def word_count(text: str) -> dict:
    """Count the words in a piece of text.

    Args:
        text: the text to measure
    """
    return {"words": len(text.split())}


def _tokens(text):          # a leading underscore marks a helper, not a tool
    return text.split()`;

// palette payload for dragging/clicking a sub-tool onto the canvas as a
// kind="tool" node (consumed by App.addNode)
export function toolDefaults(sub) {
  return {
    kind: 'tool',
    description: sub.description || '',
    tool: sub.name,
    inputs: Object.entries(sub.inputs || {}).map(([name, meta]) => ({
      name,
      type: meta.type === 'string' ? 'str' : meta.type,
      description: meta.description || '',
      required: (sub.required || []).includes(name),
    })),
  };
}

const SKILL_PLACEHOLDER = `# When to use

Describe the standard, taxonomy or house style a node should follow.

## Rules

- Be specific: this text is appended verbatim to the node's system prompt.`;

export function SkillEditor({ initial, onClose, onSaved }) {
  const [name, setName] = useState(initial?.name || '');
  const [description, setDescription] = useState(initial?.description || '');
  const [content, setContent] = useState(initial?.content || '');
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);

  const save = async (e) => {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      await api.saveSkill({ name, description, content });
      onSaved();
    } catch (err) {
      setError(err?.body?.detail || err.message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <form className="modal" onClick={(e) => e.stopPropagation()} onSubmit={save}>
        <h3>{initial ? `Skill: ${initial.name}` : 'New skill'}</h3>
        <div className="field">
          <label>Name (identifier)</label>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="risk_scoring_rubric"
            disabled={!!initial}
            required
          />
        </div>
        <div className="field">
          <label>Description (how an agent decides this skill applies)</label>
          <input
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Risk levels, score bands and the action each implies."
            required
          />
        </div>
        <div className="field">
          <label>Instructions (Markdown)</label>
          <textarea
            className="code-input"
            rows={14}
            value={content}
            onChange={(e) => setContent(e.target.value)}
            placeholder={SKILL_PLACEHOLDER}
            required
          />
          <div className="muted small">
            Saved as SKILL.md; overwriting keeps the previous version under .versions/.
          </div>
        </div>
        {error && <div className="batch-error small">{String(error)}</div>}
        <div className="modal-actions">
          <button type="button" onClick={onClose}>Cancel</button>
          <button type="submit" className="primary" disabled={saving}>
            {saving ? 'Saving…' : 'Save skill'}
          </button>
        </div>
      </form>
    </div>
  );
}

export function CustomToolEditor({ initial, onClose, onSaved }) {
  const [name, setName] = useState(initial?.name || '');
  const [code, setCode] = useState(initial?.code || '');
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);

  const save = async (e) => {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      // The code is the definition. A name is sent only because a module of
      // several functions needs something to group them under.
      await api.saveCustomTool({ name: name.trim() || undefined, code });
      onSaved();
    } catch (err) {
      setError(err?.body?.detail || err.message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <form className="modal" onClick={(e) => e.stopPropagation()} onSubmit={save}>
        <h3>Custom toolkit</h3>
        <p className="muted small">
          A tool is a calling interface with a description. Write a Python module and
          every public function in it becomes a tool: the module docstring says what
          the toolkit is, each function&apos;s docstring says when to call it, and its
          annotated arguments are the parameters. Import a library or an existing
          project and expose its API the same way.
        </p>
        <div className="field">
          <label>
            Toolkit name{' '}
            <span className="muted">(optional for a module with one function)</span>
          </label>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="text_stats" />
        </div>
        <div className="field">
          <label>Code</label>
          <textarea
            rows={14}
            className="code-input"
            value={code}
            onChange={(e) => setCode(e.target.value)}
            placeholder={CODE_PLACEHOLDER}
            required
          />
        </div>
        {error && <div className="muted small batch-error">{String(error)}</div>}
        <div className="modal-actions">
          <button type="button" onClick={onClose} disabled={saving}>Cancel</button>
          <button type="submit" className="primary" disabled={saving}>{saving ? 'Saving…' : 'Save'}</button>
        </div>
      </form>
    </div>
  );
}

// A toolkit uploaded as a folder. Zip a library or a project; name the entry
// file when it is not obvious; the entry's public functions are the tools.
export function ToolkitUpload({ onClose, onSaved }) {
  const [file, setFile] = useState(null);
  const [name, setName] = useState('');
  const [entry, setEntry] = useState('');
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);
  const [result, setResult] = useState(null);

  const save = async (e) => {
    e.preventDefault();
    if (!file) return;
    setSaving(true);
    setError(null);
    try {
      const spec = await api.uploadCustomTool(file, { name: name.trim(), entry: entry.trim() });
      setResult(spec);
      onSaved?.(spec);
    } catch (err) {
      setError(err?.body?.detail || err.message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <form className="modal" onClick={(e) => e.stopPropagation()} onSubmit={save}>
        <h3>Upload a toolkit</h3>
        <p className="muted small">
          A tool is a calling interface with a description — it need not be a
          snippet. Zip a folder: a library, a project, a package. The entry file&apos;s
          public functions become the tools; sibling modules, data files and a
          requirements.txt travel with them and are there when they run.
        </p>
        <div className="field">
          <label htmlFor="toolkit-zip">Folder (.zip)</label>
          <input id="toolkit-zip" type="file" accept=".zip" onChange={(e) => setFile(e.target.files?.[0] || null)} />
        </div>
        <div className="field">
          <label htmlFor="toolkit-name">Toolkit name <span className="muted">(optional: the folder&apos;s name)</span></label>
          <input id="toolkit-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="my_project" />
        </div>
        <div className="field">
          <label htmlFor="toolkit-entry">Entry file <span className="muted">(optional: tools.py / main.py / api.py / a package&apos;s __init__.py)</span></label>
          <input id="toolkit-entry" value={entry} onChange={(e) => setEntry(e.target.value)} placeholder="src/api.py" />
        </div>
        {result && (
          <div className="muted small">
            Saved <b>{result.name}</b>: {result.tools.map((t) => t.name).join(', ')} from {result.package.entry}
            {result.package.requirements.length > 0 && (
              <> · needs {result.package.requirements.join(', ')} — install from the list below.</>
            )}
          </div>
        )}
        {error && <div className="muted small batch-error">{String(error)}</div>}
        <div className="modal-actions">
          <button type="button" onClick={onClose} disabled={saving}>{result ? 'Done' : 'Cancel'}</button>
          {!result && <button type="submit" className="primary" disabled={saving || !file}>{saving ? 'Uploading…' : 'Upload'}</button>}
        </div>
      </form>
    </div>
  );
}

export default function ToolsPanel({ onAdd, disabled }) {
  const [uploadOpen, setUploadOpen] = useState(false);
  const [installing, setInstalling] = useState(null);
  const [installNote, setInstallNote] = useState(null);
  const [customTools, setCustomTools] = useState([]);
  const [sourceError, setSourceError] = useState(null);
  // A function marked as an input source appears in the source palette with
  // its parameters as the node's config. The toolkit is re-saved with the
  // mark; the code itself is untouched.
  const markSource = async (toolkit, fn, on) => {
    const sources = on
      ? [...new Set([...(toolkit.sources || []), fn])]
      : (toolkit.sources || []).filter((n) => n !== fn);
    try {
      await api.saveCustomTool({ name: toolkit.name, code: toolkit.code, sources });
      const r = await api.listCustomTools();
      setCustomTools(r.tools || []);
    } catch (err) {
      setSourceError(err?.body?.detail || err.message);
    }
  };
  const [editorOpen, setEditorOpen] = useState(false);
  const [skills, setSkills] = useState([]);
  const [skillEditor, setSkillEditor] = useState(null); // null | {} | skill

  const refreshSkills = () => {
    api.listSkills().then((r) => setSkills(r.skills || [])).catch(() => setSkills([]));
  };

  const deleteSkill = (name) => {
    if (!window.confirm(`Delete skill “${name}”? Workflows using it will need updating.`)) return;
    api.deleteSkill(name).then(refreshSkills).catch(e => setSourceError(e?.body?.detail || e.message));
  };

  const refreshTools = () => {
    api.listCustomTools().then((r) => setCustomTools(r.tools || [])).catch(() => {});
  };
  useEffect(() => {
    refreshSkills();
    refreshTools();
  }, []);

  const deleteTool = async (name) => {
    if (!window.confirm(`Delete custom tool “${name}”? Workflows using it will need updating.`)) return;
    try { await api.deleteCustomTool(name); refreshTools(); }
    catch (e) { setSourceError(e?.body?.detail || e.message); }
  };

  const addCustomNode = () => onAdd?.({
    type: 'custom',
    label: 'Custom node',
    defaults: {
      description: '',
      inputs: [{ name: 'input', type: 'str', description: '', required: true }],
      outputs: [{ name: 'output', type: 'str', description: '', required: true }],
      prompt: '',
      system_prompt: '',
      parse_mode: 'str',
    },
  });

  return (
    <aside className="palette">
      <div className="palette-title-row">
        <h3>Custom capabilities</h3>
      </div>
      <p className="palette-hint">
        Create a workflow step or reusable code that an LLM node can call.
      </p>
      <div className="custom-capability-actions">
        <button type="button" className="palette-custom-add" disabled={disabled} onClick={addCustomNode}>
          + Custom node
        </button>
        <button type="button" className="palette-custom-add" onClick={() => setEditorOpen(true)}>
          + Custom tool
        </button>
        <button type="button" className="palette-custom-add" onClick={() => setUploadOpen(true)}>
          + Upload folder
        </button>
      </div>
      <h3>Custom tools</h3>
      <p className="palette-hint">
        Each public function in a Python module becomes a reusable tool. Once saved,
        it appears with the built-in tools in Library.
      </p>
      {customTools.map((t) => (
        <ResourceActions key={t.name} name={t.name} items={[{ label: 'Delete custom tool…', danger: true, action: () => deleteTool(t.name) }]}><div className="palette-item">
          <div className="palette-label">
            {t.name}
            <button type="button" className="param-del" style={{ float: 'right' }} title="Delete" onClick={() => deleteTool(t.name)}>
              ✕
            </button>
          </div>
          <div className="palette-desc">{t.description}</div>
          {t.package && (
            <div className="muted small">
              📦 folder · {t.package.files} files · entry {t.package.entry}
              {(t.package.requirements || []).length > 0 && (
                <>
                  {' · needs '}{t.package.requirements.join(', ')}{' '}
                  <button
                    type="button"
                    disabled={installing === t.name}
                    onClick={async () => {
                      setInstalling(t.name); setInstallNote(null);
                      try {
                        const out = await api.installCustomToolRequirements(t.name);
                        setInstallNote(`Installed for ${t.name}: ${out.installed.join(', ') || 'nothing to install'}`);
                      } catch (err) {
                        setInstallNote(`Install failed: ${err?.body?.detail || err.message}`);
                      } finally { setInstalling(null); }
                    }}
                  >
                    {installing === t.name ? 'Installing…' : 'Install requirements'}
                  </button>
                </>
              )}
            </div>
          )}
          {(t.tools || []).map((sub) => (
            <label key={sub.name} className="tool-option" title={sub.description}>
              <input
                type="checkbox"
                aria-label={`${sub.name} as input source`}
                checked={(t.sources || []).includes(sub.name)}
                onChange={(e) => markSource(t, sub.name, e.target.checked)}
              />
              {sub.name}
              <span className="muted small"> · input source</span>
            </label>
          ))}
        </div></ResourceActions>
      ))}
      {sourceError && <div className="chat-error">{String(sourceError)}</div>}
      {installNote && <div className="muted small">{installNote}</div>}
      {customTools.length === 0 && <p className="muted small">No custom tools yet.</p>}
      <h3>Skills</h3>
      <p className="palette-hint">
        A skill is instructions a node follows, not code it calls. Attach one to
        an LLM node in the Inspector and it is appended to that node&apos;s system
        prompt at run time.
      </p>
      <button type="button" className="palette-custom-add" onClick={() => setSkillEditor({})}>
        + Skill
      </button>
      {skills.map((sk) => (
        <ResourceActions key={sk.name} name={sk.name} items={[{ label: 'Edit skill', action: () => setSkillEditor(sk) }, { label: 'Delete skill…', danger: true, action: () => deleteSkill(sk.name) }]}><div
          key={sk.name}
          className="palette-item"
          title="Click to edit"
          onClick={() => setSkillEditor(sk)}
        >
          <div className="palette-label">
            📘 {sk.name}
            <button
              type="button"
              className="param-del"
              style={{ float: 'right' }}
              title="Delete skill"
              onClick={(e) => { e.stopPropagation(); deleteSkill(sk.name); }}
            >
              ✕
            </button>
          </div>
          <div className="palette-desc">{sk.description}</div>
        </div></ResourceActions>
      ))}
      {skills.length === 0 && <p className="muted small">No skills yet.</p>}
      {uploadOpen && (
        <ToolkitUpload
          onClose={() => setUploadOpen(false)}
          onSaved={() => api.listCustomTools().then((r) => setCustomTools(r.tools || [])).catch(() => {})}
        />
      )}
      {editorOpen && (
        <CustomToolEditor
          onClose={() => setEditorOpen(false)}
          onSaved={() => { setEditorOpen(false); refreshTools(); }}
        />
      )}
      {skillEditor && (
        <SkillEditor
          initial={skillEditor.name ? skillEditor : null}
          onClose={() => setSkillEditor(null)}
          onSaved={() => { setSkillEditor(null); refreshSkills(); }}
        />
      )}
    </aside>
  );
}

