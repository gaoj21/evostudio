import React, {useEffect, useState} from 'react';
import NumberInput from '../../components/NumberInput.jsx';
import {api} from '../../api.js';

export default function DatasetInterface({code, values, onChange, onSchema, disabled, inspectCode=api.inspectDataLoaderCode, title='Dataset', providedText='Upload selection supplies resource automatically. Fill in the inputs detected from your code below.'}) {
  const [schema,setSchema]=useState(null),[error,setError]=useState('');
  useEffect(()=>{
    let active=true;setSchema(null);setError('');
    if(!code?.trim())return;
    const timer=setTimeout(async()=>{
      try {const result=await inspectCode(code);if(active){setSchema(result);onSchema(result);}}
      catch(e){if(active)setError(e.body?.detail || e.message);}
    },300);
    return()=>{active=false;clearTimeout(timer);};
  },[code,onSchema,inspectCode]);
  const update=(name,value)=>{
    const next={...values};if(value===undefined)delete next[name];else next[name]=value;onChange(next);
  };
  return <section aria-label={`${title} input interface`}><h4>Inputs</h4>
    <p className="muted small">{providedText}</p>
    {error && <p role="alert">{String(error)}</p>}
    {!schema && code?.trim() && !error && <p role="status">Analyzing code interface…</p>}
    {schema?.entrypoint && <p className="muted small">Entry point: <code>{schema.entrypoint}</code> · Other functions and classes are internal implementation.</p>}
    {schema?.warnings?.map(w=><p className="muted small" key={w}>{w}</p>)}
    {schema?.dependencies?.some(d=>!d.available) && <div role="alert" data-testid="missing-dependencies"><p className="small">Not installed in Studio's Python environment. Install, then run again:</p>{schema.dependencies.filter(d=>!d.available).map(d=><p className="small" key={d.name}>{d.kind==='spacy model'?'spaCy model':'Package'} <code>{d.name}</code>: <code style={{overflowWrap:'anywhere'}}>{d.install}</code></p>)}</div>}
    {schema?.dependencies?.some(d=>d.available) && <p className="muted small">Uses installed: {schema.dependencies.filter(d=>d.available).map(d=>d.name).join(', ')}</p>}
    {schema?.inputs?.length===0 && <p className="muted small">No additional inputs required.</p>}
    {schema?.inputs?.map(field=>{
      const value=values[field.name] ?? field.default ?? '';
      const id=`dataset-arg-${field.name}`;
      return <div className="field" key={field.name}><label htmlFor={id}>{field.name}{field.required?' *':''} <small>({field.type})</small></label>
        {field.options ? <select id={id} value={JSON.stringify(value)} disabled={disabled} onChange={e=>update(field.name,JSON.parse(e.target.value))}><option value={JSON.stringify('')}>Select…</option>{field.options.map(v=><option key={JSON.stringify(v)} value={JSON.stringify(v)}>{String(v)}</option>)}</select>
        : field.type==='bool' ? <select id={id} disabled={disabled} value={String(value)} onChange={e=>update(field.name,e.target.value===''?undefined:e.target.value==='true')}><option value="">Select…</option><option value="true">True</option><option value="false">False</option></select>
        : ['list','dict'].includes(field.type) ? <textarea id={id} key={`${field.name}:${code}`} disabled={disabled} defaultValue={value===''?'':JSON.stringify(value,null,2)} onBlur={e=>{try{update(field.name,e.target.value?JSON.parse(e.target.value):undefined);e.target.setCustomValidity('');}catch{e.target.setCustomValidity('Enter valid JSON');e.target.reportValidity();}}}/>
        : ['int','float'].includes(field.type) ? <NumberInput id={id} disabled={disabled} required={field.required} step={field.type==='int'?1:'any'} value={value} onChange={e=>update(field.name,Number(e.target.value))}/>
        : <input id={id} disabled={disabled} required={field.required} type="text" step={field.type==='int'?1:'any'} value={value} placeholder={field.name==='path'?'Paste a Workspace path':''} onChange={e=>update(field.name,['int','float'].includes(field.type)?(e.target.value===''?undefined:Number(e.target.value)):e.target.value)}/>}
      </div>;
    })}
  </section>;
}

export function DatasetOutputs({fields, mode}) {
  return <section aria-label="Dataset output interface"><h4>Outputs · one workflow record</h4>
    {!fields?.length ? <p className="muted small">Sample records to identify outputs before connecting downstream nodes.</p> : <>
      <table><thead><tr><th>Field</th><th>Type</th><th>Nullable</th><th>Example</th></tr></thead><tbody>{fields.map(f=><tr key={f.name}><td>{f.name}</td><td>{f.type}</td><td>{f.nullable?'Yes':'No'}</td><td><code style={{overflowWrap:'anywhere'}}>{JSON.stringify(f.sample)?.slice(0,160) ?? '—'}</code></td></tr>)}</tbody></table>
      <p className="muted small">These fields are applied to the canvas output ports. Map them to downstream node inputs. {mode === 'declared' ? 'Types come from OUTPUT_SCHEMA and have not been verified against data.' : 'Sampled fields may not cover every record in the dataset.'}</p>
    </>}
  </section>;
}
