import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import ChatComposer from './ChatComposer.jsx';
it('sends Enter, preserves Shift Enter and does not submit during IME composition',()=>{
 const send=vi.fn();render(<ChatComposer value="Question" onChange={()=>{}} onSend={send}/>);
 const input=screen.getByRole('textbox');
 fireEvent.keyDown(input,{key:'Enter',shiftKey:true});
 fireEvent.keyDown(input,{key:'Enter',isComposing:true});
 expect(send).not.toHaveBeenCalled();
 fireEvent.keyDown(input,{key:'Enter'});
 expect(send).toHaveBeenCalledTimes(1);
});
