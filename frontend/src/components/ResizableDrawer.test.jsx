import React from 'react';
import {render,screen,fireEvent} from '@testing-library/react';
import {it,expect} from 'vitest';
import ResizableDrawer from './ResizableDrawer.jsx';
it('resizes by keyboard and remembers height when reopened',()=>{
 localStorage.clear();
 const view=render(<ResizableDrawer>Memory</ResizableDrawer>);
 const handle=screen.getByRole('separator');
 const before=Number(handle.getAttribute('aria-valuenow'));
 fireEvent.keyDown(handle,{key:'ArrowUp'});
 expect(Number(handle.getAttribute('aria-valuenow'))).toBe(before+32);
 view.unmount();render(<ResizableDrawer>Result</ResizableDrawer>);
 expect(Number(screen.getByRole('separator').getAttribute('aria-valuenow'))).toBe(before+32);
});
