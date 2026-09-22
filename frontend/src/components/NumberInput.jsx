import React,{useEffect,useRef,useState} from 'react';

// Keep edits as text so clearing a number doesn't commit Number('') === 0.
// A blank draft restores the last valid value on blur; typing stays unrestricted.
export default function NumberInput({value,onChange,onFocus,onBlur,...props}) {
 const [draft,setDraft]=useState(value ?? '');
 const focused=useRef(false);
 useEffect(()=>{if(!focused.current)setDraft(value ?? '');},[value]);
 return <input {...props} type="number" value={draft}
  onFocus={e=>{focused.current=true;onFocus?.(e);}}
  onChange={e=>{setDraft(e.target.value);if(e.target.value!=='' && Number.isFinite(Number(e.target.value)))onChange?.(e);}}
  onBlur={e=>{focused.current=false;setDraft(value ?? '');onBlur?.(e);}}/>;
}
