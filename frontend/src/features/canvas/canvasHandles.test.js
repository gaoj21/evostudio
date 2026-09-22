import { expect, it } from 'vitest';
import { adoptUserNodes } from '@xyflow/system';
import { resourceGeometry, MEMORY_HANDLES, CHAT_HANDLES } from './canvasHandles.js';

it('keeps every port available before measurement and after asynchronous resource replacement',()=>{
 const lookup=new Map(), parents=new Map();
 const nodes=[{id:'mem:investigate',type:'memory',initialWidth:220,initialHeight:110,position:{x:0,y:0},data:{}},
 {id:'chat:one',type:'chatAgent',width:220,height:88,position:{x:300,y:200},data:{}}];
 adoptUserNodes(nodes.map(n=>resourceGeometry(n)),lookup,parents);
 expect(lookup.get(nodes[0].id).internals.handleBounds.source.map(h=>h.id)).toEqual(MEMORY_HANDLES.filter(h=>h.type==='source').map(h=>h.id));
 expect(lookup.get(nodes[1].id).internals.handleBounds.target.map(h=>h.id)).toEqual(CHAT_HANDLES.filter(h=>h.type==='target').map(h=>h.id));
 // A late memory name or Chat binding response replaces userNode before the
 // initial dimensions event is committed. No manual selection/resize needed.
 adoptUserNodes(nodes.map(n=>resourceGeometry({...n,data:{title:'Loaded'}})),lookup,parents);
 expect(lookup.get(nodes[0].id).internals.handleBounds.source.find(h=>h.id==='s-out-bottom')).toMatchObject({x:133.4,y:107});
 expect(lookup.get(nodes[1].id).internals.handleBounds.target.find(h=>h.id==='t-in')).toBeDefined();
 adoptUserNodes(nodes.map(n=>resourceGeometry(n,{width:300,height:150})),lookup,parents);
 expect(lookup.get(nodes[0].id).internals.handleBounds.source.find(h=>h.id==='s-out-bottom')).toMatchObject({x:183,y:147});
});
