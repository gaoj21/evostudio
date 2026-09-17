import React, {useEffect,useState} from 'react';
import {api} from '../../api.js';
import PythonDatasetEditor from '../data/PythonDatasetEditor.jsx';
import DatasetInterface, {DatasetOutputs} from '../data/DatasetInterface.jsx';
import JsonView from '../../components/JsonView.jsx';

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
const ignoreSchema = () => {};
const inspectConfiguration = async code => (await api.inspectToolCode(code)).configuration;

export default function PythonToolEditor({initial,onClose,onSaved}) {
 const [name,setName]=useState(initial?.name || ''),[code,setCode]=useState(initial?.code || '');
 const [config,setConfig]=useState(initial?.config || {}),[schema,setSchema]=useState(null),[dirty,setDirty]=useState(false);
 const [busy,setBusy]=useState(false),[error,setError]=useState(''),[result,setResult]=useState(null);
 const [args,setArgs]=useState('{}'),[tool,setTool]=useState('');
 useEffect(()=>{
   let current=true;setSchema(null);setResult(null);setError('');
   if(code.trim())api.inspectToolCode(code).then(value=>{if(current){setSchema(value);setTool(value.tools?.[0]?.name || '');}}).catch(e=>{if(current)setError(e.body?.detail || e.message);});
   return()=>{current=false;};
 },[code]);
 const body=()=>({...initial,name:name.trim() || undefined,code,config});
 async function save(e){
   e.preventDefault();if(dirty || !schema)return;setBusy(true);setError('');
   try{await api.saveCustomTool(body());onSaved();}catch(e){setError(e.body?.detail || e.message);}finally{setBusy(false);}
 }
 async function preview(){
   setBusy(true);setError('');setResult(null);
   try{setResult(await api.previewToolCode({...body(),tool:tool || undefined,args:JSON.parse(args)}));}
   catch(e){setError(e.body?.detail || e.message);}finally{setBusy(false);}
 }
 return <div className="modal-backdrop" onClick={onClose}><form className="modal" onClick={e=>e.stopPropagation()} onSubmit={save}>
 <h3>Custom toolkit</h3>
 <div className="field"><label htmlFor="python-tool-name">Tool / toolkit name</label><input id="python-tool-name" value={name} disabled={busy} onChange={e=>{setName(e.target.value);setResult(null);}} placeholder="word_counter"/></div>
 <PythonDatasetEditor kind="Tool" code={code} onChange={setCode} onDirtyChange={setDirty} disabled={busy} example={TOOL_EXAMPLE}/>
 {schema?.factory && <><DatasetInterface title="Tool configuration" providedText="Factory settings are stored with this tool, separate from runtime inputs supplied by canvas or Chat." code={code} inspectCode={inspectConfiguration} values={config} onChange={v=>{setConfig(v);setResult(null);}} disabled={busy} onSchema={ignoreSchema}/>
 <h4>Runtime inputs</h4><JsonView value={schema.inputs}/><DatasetOutputs fields={schema.outputs}/></>}
 {schema && !schema.factory && <><p className="muted small">Existing function toolkit interface</p><JsonView value={schema.tools}/></>}
 <details><summary>Optional execution test</summary><p className="muted small">Runs this code with your permissions and installed dependencies. It may have side effects. Saving or confirming does not execute it.</p>
 {schema?.tools && <select aria-label="Test tool" value={tool} onChange={e=>{setTool(e.target.value);setResult(null);}}>{schema.tools.map(t=><option key={t.name}>{t.name}</option>)}</select>}
 <label htmlFor="tool-test-inputs">Test inputs (JSON)</label><textarea id="tool-test-inputs" value={args} onChange={e=>{setArgs(e.target.value);setResult(null);}}/>
 <button type="button" disabled={busy || dirty || !schema} onClick={preview}>Run example</button>
 {result && <JsonView value={result}/>}</details>
 {error && <p role="alert">{String(error)}</p>}
 <div className="modal-actions"><button type="button" disabled={busy} onClick={onClose}>Cancel</button><button type="submit" className="primary" disabled={busy || dirty || !schema}>{busy?'Working…':'Save'}</button></div>
 </form></div>;
}
