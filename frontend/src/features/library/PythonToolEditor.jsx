import React, {useCallback,useEffect,useState} from 'react';
import {api} from '../../api.js';
import PythonDatasetEditor from '../data/PythonDatasetEditor.jsx';
import DatasetInterface, {DatasetOutputs} from '../data/DatasetInterface.jsx';
import JsonView from '../../components/JsonView.jsx';

// A toolkit is a Python module, written the way a DataLoader is: any imports,
// helpers and setup, then either public functions (each one a tool) or a
// class whose public methods are the tools. Docstrings and type hints are
// what the model is told; the class constructor's typed parameters are the
// configuration form.
export const FUNCTION_EXAMPLE = `"""Text statistics."""
import re
from collections import Counter

WORD = re.compile(r"\\w+")          # module-level setup runs once per load


def _words(text):                   # a leading underscore marks a helper
    return WORD.findall(text.lower())


def word_count(text: str) -> dict:
    """Count the words in a piece of text.

    Args:
        text: the text to measure
    """
    return {"words": len(_words(text))}


def most_common(text: str, n: int = 3) -> list:
    """The most frequent words of a text.

    Args:
        text: the text to search
        n: how many words to return
    """
    return [word for word, _ in Counter(_words(text)).most_common(n)]
`;

export const CLASS_EXAMPLE = `"""A keyword index over the texts it has been given."""
import re
from collections import defaultdict


class KeywordIndex:
    """Index texts and look them up by keyword.

    One instance serves every call of a run, so what one call adds
    the next can find.
    """

    def __init__(self, min_length: int = 3, case_sensitive: bool = False):
        # Typed constructor parameters are this tool's configuration.
        self.min_length = min_length
        self.case_sensitive = case_sensitive
        self.index = defaultdict(set)
        self.texts = []

    def _terms(self, text):
        words = re.findall(r"\\w+", text if self.case_sensitive else text.lower())
        return {w for w in words if len(w) >= self.min_length}

    def add(self, text: str) -> dict:
        """Add a text to the index.

        Args:
            text: the text to index
        """
        self.texts.append(text)
        for term in self._terms(text):
            self.index[term].add(len(self.texts) - 1)
        return {"id": len(self.texts) - 1, "indexed": len(self.texts)}

    def search(self, keyword: str, limit: int = 5) -> list:
        """Texts containing a keyword, oldest first.

        Args:
            keyword: the word to look up
            limit: at most this many texts
        """
        term = keyword if self.case_sensitive else keyword.lower()
        return [self.texts[i] for i in sorted(self.index.get(term, ()))[:limit]]
`;

export const FACTORY_EXAMPLE = `"""Convert amounts between units with a configurable rate table."""


class Converter:
    def __init__(self, rates):
        self.rates = rates

    def convert(self, amount: float, unit: str) -> float:
        """Convert an amount into the base unit.

        Args:
            amount: the amount to convert
            unit: its unit; must be in the configured rate table
        """
        if unit not in self.rates:
            raise ValueError(f"Unknown unit {unit!r}; known: {sorted(self.rates)}")
        return amount * self.rates[unit]


def build_tool(rates: dict, scale: float = 1.0) -> Converter:
    # build_tool's typed parameters are the configuration. It may prepare
    # anything first (open a client, load a file) and returns the object
    # whose public methods are the tools.
    return Converter({unit: rate * scale for unit, rate in rates.items()})
`;

export const PACKAGE_EXAMPLE = `"""Entry file of an uploaded folder (e.g. mylib/__init__.py).

The folder is on the import path, so sibling modules and packages import
normally - relative imports too. TOOL_CLASS names the class whose public
methods are the tools, wherever it is defined. Upload the folder (or a .zip
of it) with "Upload folder"; a requirements.txt in it is listed and can be
installed from the tool list.
"""
from .client import Client          # a class defined in a sibling module

TOOL_CLASS = Client
`;

export const TOOL_EXAMPLE = `"""Count words meeting a minimum length."""
INPUT_SCHEMA = [{"name": "text", "type": "str"}]
OUTPUT_SCHEMA = [{"name": "words", "type": "int"}]

class WordCounter:
    def __init__(self, minimum_length):
        self.minimum_length = minimum_length

    def run(self, inputs):
        return {"words": sum(len(word) >= self.minimum_length for word in inputs["text"].split())}


def build_tool(minimum_length: int = 1):
    return WordCounter(minimum_length)
`;

export const TOOL_EXAMPLES = [
  {label: 'Class with configuration', code: CLASS_EXAMPLE},
  {label: 'Functions in a module', code: FUNCTION_EXAMPLE},
  {label: 'build_tool factory', code: FACTORY_EXAMPLE},
  {label: 'Package entry (uploaded folder)', code: PACKAGE_EXAMPLE},
  {label: 'run(inputs) with declared schemas', code: TOOL_EXAMPLE},
];

const KIND_LABEL = {
  function: 'Functions: each public function is a tool.',
  class: 'Class: each public method is a tool; one instance serves every call of a run.',
  factory: 'build_tool with INPUT_SCHEMA / OUTPUT_SCHEMA: one tool, run(inputs).',
};

const argsTemplate = tool => JSON.stringify(Object.fromEntries((tool?.params || []).map(p => [p.name,
  {string:'',integer:0,number:0,boolean:false,object:{},array:[]}[p.type] ?? null])), null, 2);

export function ToolList({tools}) {
  if (!tools?.length) return null;
  return <table aria-label="Discovered tools"><thead><tr><th>Tool</th><th>Parameters</th><th>Description</th></tr></thead><tbody>
    {tools.map(t => <tr key={t.name}><td><code>{t.name}</code></td>
      <td>{(t.params || []).length ? t.params.map(p => <div key={p.name}><code>{p.name}</code>: {p.type}{p.required === false ? ' (optional)' : ''}{p.description ? <span className="muted small"> · {p.description}</span> : null}</div>) : <span className="muted small">none</span>}</td>
      <td>{t.description}</td></tr>)}
  </tbody></table>;
}

export default function PythonToolEditor({initial,onClose,onSaved}) {
 const packaged = initial?.package;
 const [name,setName]=useState(initial?.name || ''),[code,setCode]=useState(initial?.code || '');
 const [config,setConfig]=useState(initial?.config || {}),[schema,setSchema]=useState(null),[dirty,setDirty]=useState(false);
 const [busy,setBusy]=useState(false),[error,setError]=useState(''),[result,setResult]=useState(null);
 const [args,setArgs]=useState('{}'),[tool,setTool]=useState('');
 const extra = packaged ? {name: initial.name} : {};
 const chooseTool = (value, tools) => {
   setTool(value);setResult(null);
   const found=(tools || schema?.tools || []).find(t=>t.name===value);
   if(found)setArgs(argsTemplate(found));
 };
 const inspect = async (execute=false) => {
   const value = await api.inspectToolCode(code, execute ? {...extra, execute:true, config} : extra);
   setSchema(value);
   if(value.tools?.length)chooseTool(value.tools[0].name, value.tools);
   return value;
 };
 useEffect(()=>{
   let current=true;setSchema(null);setResult(null);setError('');
   if(code.trim())api.inspectToolCode(code, extra).then(value=>{if(current){setSchema(value);if(value.tools?.length)chooseTool(value.tools[0].name,value.tools);}}).catch(e=>{if(current)setError(e.body?.detail || e.message);});
   return()=>{current=false;};
   // eslint-disable-next-line react-hooks/exhaustive-deps
 },[code]);
 const configuration = useCallback(async()=>schema?.configuration,[schema]);
 const ignoreSchema = useCallback(()=>{},[]);
 const body=()=>{
   const {tools:_tools,description:_description,verification:_verification,...kept}=initial || {};
   return {...kept,name:name.trim() || undefined,code,config,...(packaged && code!==initial.code ? {edit_entry:true} : {})};
 };
 async function save(e){
   e.preventDefault();if(dirty || !schema)return;setBusy(true);setError('');
   try{await api.saveCustomTool(body());onSaved();}catch(e){setError(e.body?.detail || e.message);}finally{setBusy(false);}
 }
 async function discover(){
   setBusy(true);setError('');
   try{await inspect(true);}catch(e){setError(e.body?.detail || e.message);}finally{setBusy(false);}
 }
 async function preview(){
   setBusy(true);setError('');setResult(null);
   try{setResult(await api.previewToolCode({...body(),tool:tool || undefined,args:JSON.parse(args)}));}
   catch(e){setError(e.body?.detail || e.message);}finally{setBusy(false);}
 }
 const hasConfiguration = schema?.configuration && (schema.kind === 'factory' || schema.factory || schema.configuration.inputs?.length > 0);
 return <div className="modal-backdrop" onClick={onClose}><form className="modal" onClick={e=>e.stopPropagation()} onSubmit={save}>
 <h3>Custom toolkit</h3>
 <div className="field"><label htmlFor="python-tool-name">Tool / toolkit name</label><input id="python-tool-name" value={name} disabled={busy || !!packaged} onChange={e=>{setName(e.target.value);setResult(null);}} placeholder="text_tools"/></div>
 {packaged && <p role="note" className="muted small">Uploaded folder · entry <code>{packaged.entry}</code> · {packaged.files} files. Saving an edit writes this code back into <code>{packaged.entry}</code> in the folder; its other files stay as uploaded. The code runs inside the folder, so sibling modules import normally.</p>}
 <PythonDatasetEditor kind="Tool" code={code} onChange={setCode} onDirtyChange={setDirty} disabled={busy} examples={TOOL_EXAMPLES}/>
 {schema && <section aria-label="Tool interface">
   <h4>Interface</h4>
   <p className="muted small">{KIND_LABEL[schema.kind] || ''}{schema.class ? <> Class <code>{schema.class}</code>.</> : null}{schema.description ? <> {schema.description}</> : null}</p>
   {schema.needs_discovery && <div role="status"><p className="muted small">This class is imported, so its methods can only be read by importing the module. That runs the module&apos;s top-level code (not the tools) in an isolated process.</p><button type="button" disabled={busy || dirty} onClick={discover}>Read tools by importing</button></div>}
   {schema.kind === 'factory' || schema.factory ? <><h4>Runtime inputs</h4><JsonView value={schema.inputs}/><DatasetOutputs fields={schema.outputs}/></> : <ToolList tools={schema.tools}/>}
 </section>}
 {hasConfiguration && <DatasetInterface title="Tool configuration" providedText="Configuration is set once and stored with this tool: build_tool's or the class constructor's typed parameters. Tool arguments come from the canvas or the model at call time." code={code} inspectCode={configuration} values={config} onChange={v=>{setConfig(v);setResult(null);}} disabled={busy} onSchema={ignoreSchema}/>}
 <details><summary>Test call</summary><p className="muted small">Runs this code with your permissions and installed dependencies, in an isolated process. It may have side effects. Confirming code or saving does not run it.</p>
 {schema?.tools?.length > 0 && <select aria-label="Test tool" value={tool} onChange={e=>chooseTool(e.target.value)}>{schema.tools.map(t=><option key={t.name}>{t.name}</option>)}</select>}
 <label htmlFor="tool-test-inputs">Test inputs (JSON)</label><textarea id="tool-test-inputs" value={args} onChange={e=>{setArgs(e.target.value);setResult(null);}}/>
 <button type="button" disabled={busy || dirty || !schema} onClick={preview}>Run example</button>
 {result && <><JsonView value={result.result}/>{result.logs ? <><p className="muted small">Printed output</p><pre>{result.logs}</pre></> : null}</>}</details>
 {error && <pre role="alert" style={{whiteSpace:'pre-wrap'}}>{String(error)}</pre>}
 <div className="modal-actions"><button type="button" disabled={busy} onClick={onClose}>Cancel</button><button type="submit" className="primary" disabled={busy || dirty || !schema}>{busy?'Working…':'Save'}</button></div>
 </form></div>;
}
