import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { beforeEach, expect, it } from 'vitest';
import { chatHistoryKey, useChatHistory } from './chatSession.js';

function Session({ page }) {
 const [messages, setMessages] = useChatHistory(chatHistoryKey('g', page, 'all', 'all'));
 return <><p>{messages.map(m => m.content).join('|')}</p><button onClick={() => setMessages(old => [...old,{role:'user',content:page}])}>Add</button></>;
}
beforeEach(() => localStorage.clear());
it('preserves legacy history while switching page contexts without overwriting either',()=>{
 localStorage.setItem('evoagentx-studio:chat:g',JSON.stringify([{role:'user',content:'Old canvas'}]));
 localStorage.setItem('result-chat:g:all:all',JSON.stringify([{role:'user',content:'Old results'}]));
 const view=render(<Session page="canvas"/>);
 expect(screen.getByText('Old canvas')).toBeInTheDocument();
 view.rerender(<Session page="results"/>);
 expect(screen.getByText('Old results')).toBeInTheDocument();
 fireEvent.click(screen.getByText('Add'));
 view.rerender(<Session page="canvas"/>);
 expect(screen.getByText('Old canvas')).toBeInTheDocument();
 view.rerender(<Session page="results"/>);
 expect(screen.getByText('Old results|results')).toBeInTheDocument();
});
it('migrates legacy messages once, renames and deletes sessions without losing the others',()=>{
 localStorage.setItem('evoagentx-studio:chat:g',JSON.stringify([{role:'user',content:'Legacy conversation'}]));
 let manager;
 function Harness(){const [messages,,controls]=useChatHistory(chatHistoryKey('g'));manager=controls;return <><p>{messages.map(m=>m.content).join('|')}</p><button onClick={controls.create}>New</button><button onClick={()=>controls.rename(controls.activeId,'Named session')}>Rename</button><button onClick={()=>controls.remove(controls.activeId)}>Delete</button></>;}
 const view=render(<Harness/>);
 expect(manager.sessions).toHaveLength(1);
 fireEvent.click(screen.getByText('New'));
 expect(manager.sessions).toHaveLength(2);
 fireEvent.click(screen.getByText('Rename'));
 expect(manager.sessions.find(s=>s.id===manager.activeId).title).toBe('Named session');
 fireEvent.click(screen.getByText('Delete'));
 expect(screen.getByText('Legacy conversation')).toBeInTheDocument();
 view.unmount();render(<Harness/>);
 expect(manager.sessions).toHaveLength(1);
 expect(screen.getByText('Legacy conversation')).toBeInTheDocument();
});
it('retains all sessions when a workflow is renamed',async()=>{
 const {renameChatHistoryKey,saveChatHistory,loadChatHistory}=await import('./chatSession.js');
 let manager;
 function Harness(){const [,setMessages,controls]=useChatHistory(chatHistoryKey('old'));manager=controls;return <><button onClick={()=>setMessages([{role:'user',content:'First'}])}>Write</button><button onClick={controls.create}>New</button></>;}
 const view=render(<Harness/>);
 fireEvent.click(screen.getByText('Write'));
 fireEvent.click(screen.getByText('New'));
 const activeId=manager.activeId;
 view.unmount();
 renameChatHistoryKey(chatHistoryKey('old'),chatHistoryKey('new'));
 saveChatHistory(chatHistoryKey('new'),[{role:'assistant',content:'Rename finished'}]);
 expect(loadChatHistory(chatHistoryKey('new'))[0].content).toBe('Rename finished');
 const store=JSON.parse(localStorage.getItem(`${chatHistoryKey('new')}:sessions`));
 expect(store.sessions).toHaveLength(2);
 expect(store.activeId).toBe(activeId);
 expect(store.sessions.some(s=>s.messages[0]?.content==='First')).toBe(true);
 expect(localStorage.getItem(`${chatHistoryKey('old')}:sessions`)).toBeNull();
});
it('collects old record and batch histories into one task list only once',async()=>{
 const {resultChatKey}=await import('./chatSession.js');
 localStorage.setItem('result-chat:g:current:r1',JSON.stringify([{role:'user',content:'First record history'}]));
 localStorage.setItem('result-chat:g:batch:b1',JSON.stringify([{role:'user',content:'Batch history'}]));
 let manager;
 function Unified(){const [,,controls]=useChatHistory(resultChatKey('g'));manager=controls;return <p>Unified</p>;}
 const view=render(<Unified/>);
 expect(manager.sessions).toHaveLength(2);
 expect(manager.sessions.find(s=>s.title==='Batch history').context).toEqual({scope:'batch',batch_id:'b1'});
 expect(manager.sessions.find(s=>s.title==='First record history').context).toEqual({scope:'current',run_id:'r1'});
 view.unmount();render(<Unified/>);
 expect(manager.sessions).toHaveLength(2);
});
