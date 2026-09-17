import React from 'react';
import {render,screen,waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {describe,it,expect,vi,beforeEach} from 'vitest';
import DataLoaderInput from './DataLoaderInput.jsx';
import {api} from '../../api.js';
vi.mock('../../api.js',()=>({api:{listDataResources:vi.fn(),listCustomTools:vi.fn(),uploadDataResource:vi.fn(),previewDataLoader:vi.fn(),deleteDataResource:vi.fn()}}));
beforeEach(()=>{vi.clearAllMocks();api.listDataResources.mockResolvedValue({resources:[{id:'data',name:'Folder',files:[{path:'a.json'}]}]});api.listCustomTools.mockResolvedValue({tools:[]});});
describe('DataLoader input',()=>{
 it('previews prepared records and updates typed output fields',async()=>{
  const onChange=vi.fn();const fields=[{name:'amount',type:'int',required:false}];
  api.previewDataLoader.mockResolvedValue({fields,raw_records:3,output_records:2,preview:[{amount:1}],snapshot:'fixed'});
  render(<DataLoaderInput config={{type:'dataloader',resource_id:'data',n:0}} onChange={onChange}/>);
  await userEvent.click(screen.getByText('3. Preview and update output fields'));
  expect(await screen.findByText('3 raw → 2 output records')).toBeTruthy();
  expect(onChange).toHaveBeenCalledWith(expect.objectContaining({preview_snapshot:'fixed'}),fields);
 });
 it('shows only the PyTorch editor with a directory upload picker',async()=>{
  const {container}=render(<DataLoaderInput config={{loader:'python'}} onChange={vi.fn()}/>);
  expect(container.querySelector('input[webkitdirectory]')).toBeTruthy();
  expect(screen.queryByLabelText('2. DataLoader')).toBeNull();
  expect(screen.queryByText('Custom reader tool')).toBeNull();
  expect(screen.queryByText('Transform tool')).toBeNull();
  expect(screen.getByLabelText('Python Dataset code')).toBeTruthy();
  await waitFor(()=>expect(api.listDataResources).toHaveBeenCalled());
 });
});
