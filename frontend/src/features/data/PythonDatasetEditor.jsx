import React, {useEffect, useRef, useState} from 'react';

export const DATASET_EXAMPLE = `from pathlib import Path
from torch.utils.data import IterableDataset
from backend.features.data.json_stream import read_json_records


class JsonRecords(IterableDataset):
    def __init__(self, resource, path, split):
        root = Path(resource["root"])
        self.path = Path(path) if path else root
        if not self.path.is_absolute():
            self.path = root / self.path
        self.split = split
        if split and self.path.is_dir() and (self.path / split).is_dir():
            self.path = self.path / split
            self.split = ""
        if not self.path.exists():
            raise FileNotFoundError(str(self.path))

    def __iter__(self):
        paths = [self.path] if self.path.is_file() else self.path.rglob("*.json")
        for path in paths:
            for row in read_json_records(path):
                if self.split:
                    if "split" not in row:
                        raise ValueError("Split needs a matching subfolder or a split field")
                    if row["split"] != self.split:
                        continue
                yield row


def build_dataset(resource, path: str = "", split: str = ""):
    # Keep construction lazy. Studio controls sample count and batch size.
    return JsonRecords(resource, path, split)
`;

export const AGGREGATE_EXAMPLE = `"""One item per entity per period, from any JSON / JSONL records.

For example a collected API source saved as a data resource: every record of
the same entity (customer, device, ticker, ...) within the same day, week or
month becomes one item, so a workflow runs once per entity per period with
all of that period's material. Items come out in period order, which is what
"Execution grouping: by week" needs. Grouping reads the whole file once.
"""
import json
from datetime import date, timedelta
from pathlib import Path
from torch.utils.data import Dataset

OUTPUT_SCHEMA = [
    {"name": "entity", "type": "str"},
    {"name": "period_start", "type": "str"},
    {"name": "period_end", "type": "str"},
    {"name": "text", "type": "str"},
    {"name": "records", "type": "list"},
    {"name": "count", "type": "int"},
]


def read_records(path):
    text = Path(path).read_text(encoding="utf-8-sig").strip()
    if not text:
        return []
    if text[0] == "[":
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def period_bounds(day, period):
    if period == "day":
        return day, day
    if period == "week":
        start = day - timedelta(days=day.weekday())
        return start, start + timedelta(days=6)
    start = day.replace(day=1)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return start, end


class ByEntityAndPeriod(Dataset):
    def __init__(self, rows, entity_field, date_field, text_field, period):
        if period not in ("day", "week", "month"):
            raise ValueError("period must be day, week or month")
        groups = {}
        for row in rows:
            if date_field not in row:
                raise ValueError(f"Record has no {date_field!r} field: {sorted(row)[:8]}")
            day = date.fromisoformat(str(row[date_field])[:10])
            entity = str(row.get(entity_field, "")) if entity_field else ""
            start, end = period_bounds(day, period)
            groups.setdefault((entity, start, end), []).append(row)
        self.items = []
        for (entity, start, end), members in groups.items():
            members.sort(key=lambda r: str(r[date_field]))
            lines = [f"[{str(r[date_field])[:10]}] {r.get(text_field, '')}" for r in members] if text_field else []
            self.items.append({
                "entity": entity,
                "period_start": start.isoformat(),
                "period_end": end.isoformat(),
                "text": "\\n".join(lines),
                "records": members,
                "count": len(members),
            })
        self.items.sort(key=lambda item: (item["period_end"], item["entity"]))

    def __len__(self):
        return len(self.items)

    def __getitem__(self, index):
        return self.items[index]


def build_dataset(resource, date_field: str, entity_field: str = "", text_field: str = "",
                  period: str = "week", file: str = ""):
    # entity_field empty: one item per period for all records together.
    # text_field empty: "text" stays empty and "records" carries everything.
    root = Path(resource["root"])
    paths = [root / file] if file else [root / f["path"] for f in resource["files"]
                                        if f["path"].endswith((".json", ".jsonl"))]
    rows = [row for path in paths for row in read_records(path)]
    return ByEntityAndPeriod(rows, entity_field, date_field, text_field, period)
`;

export const DATASET_EXAMPLES = [
  { label: 'Read JSON records', code: DATASET_EXAMPLE },
  { label: 'Group by entity and period', code: AGGREGATE_EXAMPLE },
];

export default function PythonDatasetEditor({code, onChange, disabled, kind='Dataset', example=DATASET_EXAMPLE, examples, onDirtyChange}) {
  const choices = examples || (kind === 'Dataset' ? DATASET_EXAMPLES : [{ label: 'Example', code: example }]);
  const [choice, setChoice] = useState(0);
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
    {kind==='Dataset' ? <p className="muted small">Multiple classes and helper functions are allowed. Studio calls only the single top-level build_dataset factory. Paste code or drop a .py file here. Define build_dataset(resource, path: str, split: str = "dev") returning a PyTorch Dataset or IterableDataset. Typed parameters generate the input form; legacy config parameters are also supported. Each item must be a JSON-compatible dictionary. Studio creates the DataLoader and keeps the final partial batch. The Sample action reads one item to identify outputs; your Dataset constructor must avoid loading the whole file.</p> : kind==='Tool' ? <p className="muted small">A whole module: any imports (installed packages, sibling modules of an uploaded folder), helper functions and classes, module-level setup. Either every public function is a tool, or one class is the toolkit — each public method is a tool, and the class constructor&apos;s typed parameters (or build_tool&apos;s) are the configuration. TOOL_CLASS = &lt;class&gt; names it when the class is imported or there are several. Docstrings and type hints are what the model is told. The older build_tool with INPUT_SCHEMA / OUTPUT_SCHEMA and run(inputs) keeps working.</p> : <p className="muted small">Multiple classes and helper functions are allowed. Define one top-level build_evaluator with typed parameters, returning an object with evaluate(records). Only factory parameters generate the form. The method returns metrics with optional records, coverage and details. Legacy top-level evaluate(records, ...) is supported; do not define both entrypoints.</p>}
    {kind==='Dataset' && <details><summary>Declare outputs without loading data</summary><pre>{`OUTPUT_SCHEMA = [
    {"name": "text", "type": "str"},
    {"name": "label", "type": "int", "nullable": True},
]`}</pre><p className="muted small">Add this literal list at the top level of your Python file using your actual output fields. Supported types: str, int, float, bool, list, dict, any. This declares the interface; it does not validate actual records.</p></details>}
    <textarea id="loader-python-code" spellCheck={false} rows={18} style={{fontFamily:'monospace',minHeight:260,resize:'vertical'}} value={draft} disabled={disabled} onChange={e=>setDraft(e.target.value)} placeholder={kind==='Dataset'?"from torch.utils.data import Dataset…":"def evaluate(records):…"}/>
    <input ref={input} aria-label={`Upload Python ${kind}`} type="file" accept=".py" hidden onChange={e=>{load(e.target.files[0]);e.target.value='';}}/>
    <div className="input-actions"><button type="button" disabled={disabled} onClick={()=>input.current.click()}>Upload .py</button>{choices.length > 1 && <select aria-label="Example" value={choice} disabled={disabled} onChange={e=>setChoice(Number(e.target.value))}>{choices.map((c, i) => <option key={c.label} value={i}>{c.label}</option>)}</select>}<button type="button" disabled={disabled} onClick={()=>{if(!draft || window.confirm('Replace the current draft with the example?'))setDraft(choices[choice].code);}}>Load example</button></div>
    <div className="input-actions"><button type="button" className="primary" disabled={disabled || !dirty || !draft.trim()} onClick={()=>{onChange(draft);setError('');}}>Confirm code</button><button type="button" disabled={disabled || !dirty} onClick={()=>{setDraft(code || '');setError('');}}>Discard changes</button></div>
    <p role="status" className="muted small">{dirty ? 'Unconfirmed draft. Confirm code to update the parameter interface. Preview is disabled until you confirm or discard changes.' : code ? 'Code confirmed. Configure the inputs, then preview to verify outputs.' : 'Paste or upload Python code, then click Confirm code.'}</p>
    {kind==='Dataset' ? <p className="muted small">resource.root is the uploaded folder; resource.files lists relative file paths. Reader config is passed as config. Code is saved with this Input; Use Sample 1 record to execute the Dataset and apply detected output fields to the canvas. Use installed Python packages.</p> : kind==='Tool' ? <p className="muted small">Confirming code only reads the interface; nothing runs. Test inputs and Run example execute it in an isolated process, and errors name the line in your code with whatever it printed. Configuration is shared by every caller of this saved tool; save under another name for different settings. Within one run a class toolkit keeps one instance, so its state carries from one call to the next.</p> : <p className="muted small">records contains complete saved inputs, node outputs, final results and errors, including unconnected nodes. Your code owns grouping and scoring. Code is saved in this node; installed Python packages are available.</p>}
    {error && <p role="alert">{error}</p>}
  </div>;
}
