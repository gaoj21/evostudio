import React from 'react';
import {render,screen,fireEvent,waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {it,expect,vi} from 'vitest';
import PythonDatasetEditor from './PythonDatasetEditor.jsx';

it('accepts a dropped Python file as editable code',async()=>{
  const onChange=vi.fn();render(<PythonDatasetEditor code="" onChange={onChange}/>);
  const code='def build_dataset(resource, config): return MyDataset(resource)';
  fireEvent.drop(screen.getByLabelText('Python Dataset code'),{dataTransfer:{files:[new File([code],'reader.py',{type:'text/x-python'})]}});
  await waitFor(()=>expect(screen.getByLabelText('Python Dataset code')).toHaveValue(code));
  expect(onChange).not.toHaveBeenCalled();
  await userEvent.click(screen.getByText('Confirm code'));
  expect(onChange).toHaveBeenCalledWith(code);
});
it('loads a real Dataset example and rejects non-Python files',async()=>{
  const onChange=vi.fn();render(<PythonDatasetEditor code="" onChange={onChange}/>);
  await userEvent.click(screen.getByText('Load example'));
  expect(onChange).not.toHaveBeenCalled();
  await userEvent.click(screen.getByText('Confirm code'));
  expect(onChange).toHaveBeenCalledWith(expect.stringContaining('from torch.utils.data import Dataset'));
  fireEvent.drop(screen.getByLabelText('Python Dataset code'),{dataTransfer:{files:[new File(['bad'],'reader.json')]}});
  expect(await screen.findByRole('alert')).toHaveTextContent('Choose a Python');
});

it('keeps pasted edits in a draft and supports discarding them',async()=>{
 const onChange=vi.fn(),onDirtyChange=vi.fn();
 render(<PythonDatasetEditor code="original" onChange={onChange} onDirtyChange={onDirtyChange}/>);
 fireEvent.change(screen.getByLabelText('Python Dataset code'),{target:{value:'modified'}});
 expect(onChange).not.toHaveBeenCalled();
 expect(onDirtyChange).toHaveBeenLastCalledWith(true);
 await userEvent.click(screen.getByText('Discard changes'));
 expect(screen.getByLabelText('Python Dataset code')).toHaveValue('original');
 expect(onDirtyChange).toHaveBeenLastCalledWith(false);
 expect(onChange).not.toHaveBeenCalled();
});
