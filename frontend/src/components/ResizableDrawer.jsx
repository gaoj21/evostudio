import React, {useEffect, useRef, useState} from 'react';

export default function ResizableDrawer({children}) {
  const clamp = value => Math.max(160, Math.min(window.innerHeight * .85, value));
  const [height,setHeight] = useState(()=>{
    try {return clamp(Number(localStorage.getItem('studio-drawer-height')) || window.innerHeight * .4);}
    catch {return window.innerHeight * .4;}
  });
  const drag=useRef(null);
  const resize = value => setHeight(clamp(value));
  useEffect(()=>{try{localStorage.setItem('studio-drawer-height',String(height));}catch{}},[height]);
  useEffect(()=>{const fit=()=>setHeight(h=>clamp(h));window.addEventListener('resize',fit);return()=>window.removeEventListener('resize',fit);},[]);
  return <div className="drawer" style={{height,maxHeight:'85vh'}}>
    <div role="separator" tabIndex={0} aria-label="Resize results and memory panel" aria-orientation="horizontal" aria-valuenow={Math.round(height)} aria-valuemin={160} aria-valuemax={Math.round(window.innerHeight*.85)} className="drawer-resizer"
      onPointerDown={e=>{drag.current={y:e.clientY,height};e.currentTarget.setPointerCapture(e.pointerId);e.preventDefault();}}
      onPointerMove={e=>{if(drag.current)resize(drag.current.height+drag.current.y-e.clientY);}}
      onPointerUp={()=>{drag.current=null;}} onPointerCancel={()=>{drag.current=null;}} onLostPointerCapture={()=>{drag.current=null;}}
      onKeyDown={e=>{if(['ArrowUp','ArrowDown'].includes(e.key)){e.preventDefault();resize(height+(e.key==='ArrowUp'?32:-32));}}}/>
    {children}
  </div>;
}
