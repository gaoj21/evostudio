import React, {useEffect, useRef, useState} from 'react';

export const DATASET_EXAMPLE = `import json
from pathlib import Path
from torch.utils.data import Dataset


class JsonRecords(Dataset):
    def __init__(self, resource, config):
        root = Path(resource["root"])
        selected = Path(config.get("path") or root)
        if not selected.is_absolute():
            selected = root / selected
        split = config.get("split") or ""
        if split and selected.is_dir() and (selected / split).is_dir():
            selected = selected / split
            split = ""  # Already selected by directory.
        if not selected.exists():
            raise FileNotFoundError(str(selected))
        paths = [selected] if selected.is_file() else sorted(selected.rglob("*.json"))
        self.records = []
        for path in paths:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            rows = value if isinstance(value, list) else [value]
            if split:
                if any("split" not in row for row in rows):
                    raise ValueError("Split needs a matching subfolder or a split field")
                rows = [row for row in rows if row["split"] == split]
            self.records.extend(rows)

    def __len__(self):
        return len(self.records)

    def __getitem__(self, index):
        return self.records[index]


def build_dataset(resource, path: str = "", split: str = ""):
    # Named, typed parameters become editable interface fields.
    # resource is provided by the selected upload automatically.
    return JsonRecords(resource, {"path": path, "split": split})
`;

export default function PythonDatasetEditor({code, onChange, disabled, kind='Dataset', example=DATASET_EXAMPLE, onDirtyChange}) {
  const input = useRef(null);
  const [error, setError] = useState('');
  const [draft, setDraft] = useState(code || '');
  const dirty = draft !== (code || '');
  useEffect(()=>{setDraft(code || '');setError('');},[code]);
  useEffect(()=>{onDirtyChange?.(dirty);},[dirty,onDirtyChange]);
  async function load(file) {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith('.py')) {setError('Choose a Python (.py) file.');return;}
    try {
      const text = await new Promise((resolve,reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(new Error('Could not read this file.'));
        reader.readAsText(file);
      });
      setDraft(text);setError('');
    } catch(e) {setError(e.message);}
  }
  return <div className="field" onDragOver={e=>{e.preventDefault();e.stopPropagation();}} onDrop={e=>{e.preventDefault();e.stopPropagation();if(!disabled)load(e.dataTransfer.files[0]);}}>
    <label htmlFor="loader-python-code">Python {kind} code</label>
    {kind==='Dataset' ? <p className="muted small">Multiple classes and helper functions are allowed. Studio calls only the single top-level build_dataset factory. Paste code or drop a .py file here. Define build_dataset(resource, path: str, split: str = "dev") returning a PyTorch Dataset or IterableDataset. Typed parameters generate the input form; legacy config parameters are also supported. Each item must be a JSON-compatible dictionary. Studio creates the DataLoader and keeps the final partial batch.</p> : <p className="muted small">Multiple classes and helper functions are allowed. Define one top-level build_evaluator with typed parameters, returning an object with evaluate(records). Only factory parameters generate the form. The method returns metrics with optional records, coverage and details. Legacy top-level evaluate(records, ...) is supported; do not define both entrypoints.</p>}
    <textarea id="loader-python-code" spellCheck={false} rows={18} style={{fontFamily:'monospace',minHeight:260,resize:'vertical'}} value={draft} disabled={disabled} onChange={e=>setDraft(e.target.value)} placeholder={kind==='Dataset'?"from torch.utils.data import Dataset…":"def evaluate(records):…"}/>
    <input ref={input} aria-label={`Upload Python ${kind}`} type="file" accept=".py" hidden onChange={e=>{load(e.target.files[0]);e.target.value='';}}/>
    <div className="input-actions"><button type="button" disabled={disabled} onClick={()=>input.current.click()}>Upload .py</button><button type="button" disabled={disabled} onClick={()=>{if(!draft || window.confirm('Replace the current draft with the example?'))setDraft(example);}}>Load example</button></div>
    <div className="input-actions"><button type="button" className="primary" disabled={disabled || !dirty || !draft.trim()} onClick={()=>{onChange(draft);setError('');}}>Confirm code</button><button type="button" disabled={disabled || !dirty} onClick={()=>{setDraft(code || '');setError('');}}>Discard changes</button></div>
    <p role="status" className="muted small">{dirty ? 'Unconfirmed draft. Confirm code to update the parameter interface. Preview is disabled until you confirm or discard changes.' : code ? 'Code confirmed. Configure the inputs, then preview to verify outputs.' : 'Paste or upload Python code, then click Confirm code.'}</p>
    {kind==='Dataset' ? <p className="muted small">resource.root is the uploaded folder; resource.files lists relative file paths. Reader config is passed as config. Code is saved with this Input; preview executes it and updates output fields. Use installed Python packages.</p> : <p className="muted small">records contains complete saved inputs, node outputs, final results and errors, including unconnected nodes. Your code owns grouping and scoring. Code is saved in this node; installed Python packages are available.</p>}
    {error && <p role="alert">{error}</p>}
  </div>;
}
