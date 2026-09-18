import React,{useState} from 'react';
import {render,screen,fireEvent} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {it,expect} from 'vitest';
import NumberInput from './NumberInput.jsx';
it('allows clearing 4 and typing 5 without inserting a zero',async()=>{
 function Form(){const [n,setN]=useState(4);return <><NumberInput aria-label="number" value={n} onChange={e=>setN(Number(e.target.value))}/><output>{n}</output></>}
 render(<Form/>);const input=screen.getByLabelText('number');
 await userEvent.clear(input);expect(input).toHaveValue(null);
 expect(screen.getByRole('status')).toHaveTextContent('4');
 await userEvent.type(input,'5');expect(input).toHaveValue(5);
 fireEvent.blur(input);expect(input).toHaveValue(5);
});
