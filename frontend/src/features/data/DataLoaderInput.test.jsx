import React from 'react';
import {render,screen,waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {describe,it,expect,vi,beforeEach} from 'vitest';
import DataLoaderInput from './DataLoaderInput.jsx';
import {api} from '../../api.js';
vi.mock('../../api.js',()=>({api:{listDataResources:vi.fn(),listCustomTools:vi.fn(),uploadDataResource:vi.fn(),previewDataLoader:vi.fn(),deleteDataResource:vi.fn(),attachDataResource:vi.fn(),listSourceTypes:vi.fn(),inspectDataLoaderCode:vi.fn()}}));
beforeEach(()=>{vi.clearAllMocks();api.inspectDataLoaderCode.mockResolvedValue({inputs:[],warnings:[]});api.listDataResources.mockResolvedValue({resources:[{id:'data',name:'Folder',files:[{path:'a.json'}]}]});api.listCustomTools.mockResolvedValue({tools:[]});api.listSourceTypes.mockResolvedValue({source_types:[{type:'http_api'},{type:'user_dataset',local:true},{type:'project_feed',local:true,project:'demo'}]});});
describe('DataLoader input',()=>{
 it('reads declared fields and applies them to the canvas',async()=>{
  const onChange=vi.fn();const fields=[{name:'amount',type:'int',required:false}];
  api.previewDataLoader.mockResolvedValue({fields,preview_mode:'declared',sample_count:0,preview:[],snapshot:null});
  render(<DataLoaderInput config={{type:'dataloader',loader:'python',code:'def build_dataset(resource): pass',resource_id:'data',n:0}} onChange={onChange}/>);
  await userEvent.click(screen.getByText('3. Read output interface'));
  expect(await screen.findByText(/1 output field\(s\) applied to this node/)).toBeTruthy();
  expect(onChange).toHaveBeenCalledWith(expect.objectContaining({preview_snapshot:null,output_schema_mode:'declared'}),fields);
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

it('mounts a historical dataset before selecting it for an unsaved canvas',async()=>{
 api.attachDataResource.mockResolvedValue({});
 const onChange=vi.fn();
 render(<DataLoaderInput config={{loader:'python'}} getGraph={()=>({id:'task'})} onChange={onChange}/>);
 await screen.findByText('Folder · 1 files');
 await userEvent.selectOptions(screen.getByLabelText('1. Data resource'),'data');
 await waitFor(()=>expect(api.attachDataResource).toHaveBeenCalledWith('data','task'));
 await waitFor(()=>expect(onChange).toHaveBeenCalledWith(expect.objectContaining({resource_id:'data'}),[]));
});
describe('DataLoader input settings',()=>{
 it('lets the time allowed per batch be set',async()=>{
  const onChange=vi.fn();
  render(<DataLoaderInput config={{loader:'python',resource_id:'data'}} onChange={onChange}/>);
  const field=screen.getByLabelText('Seconds allowed per batch');
  expect(field).toHaveValue(120);
  await userEvent.clear(field);await userEvent.type(field,'600');
  expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({batch_timeout:600}),undefined);
 });
 it('says an API-backed Input is collected first and saved as a resource',async()=>{
  render(<DataLoaderInput config={{loader:'source',source_config:{type:'http_api',url:'https://x'}}} onChange={vi.fn()}/>);
  expect(await screen.findByTestId('api-source-note')).toHaveTextContent('collects it first');
 });
 it('does not show that note for types the schema marks local',async()=>{
  for (const type of ['user_dataset','project_feed']) {
   const {unmount}=render(<DataLoaderInput config={{loader:'source',source_config:{type}}} onChange={vi.fn()}/>);
   await waitFor(()=>expect(api.listSourceTypes).toHaveBeenCalled());
   await new Promise(r=>setTimeout(r,0));
   expect(screen.queryByTestId('api-source-note')).toBeNull();
   unmount();
  }
 });
});


it('keeps the editor open and shows applied outputs after the parent updates', async()=>{
 const fields=[{name:'amount',type:'float',required:true,nullable:false}];
 api.previewDataLoader.mockResolvedValue({fields,preview:[],preview_mode:'declared',sample_count:0,snapshot:null});
 function StatefulInput() {
   const [config,setConfig]=React.useState({type:'dataloader',loader:'python',code:'OUTPUT_SCHEMA = []\ndef build_dataset(resource): pass',read_batch_size:4});
   const [outputs,setOutputs]=React.useState([]);
   return <><DataLoaderInput config={config} onChange={(next,fields)=>{setConfig(next);if(fields)setOutputs(fields);}}/>
     <output data-testid="canvas-fields">{outputs.map(f=>f.name).join(',')}</output></>;
 }
 const {container}=render(<StatefulInput/>);
 const codeDisclosure=screen.getByText('Dataset code').closest('details');
 expect(codeDisclosure.open).toBe(true);
 const button=screen.getByRole('button',{name:'3. Read output interface'});
 // Static inspection does not require a resource or form initialization.
 expect(button).toBeEnabled();
 await userEvent.click(button);
 expect(await screen.findByText(/1 output field\(s\) applied/)).toBeVisible();
 expect(screen.getByTestId('canvas-fields')).toHaveTextContent('amount');
 expect(codeDisclosure.open).toBe(true);
 expect(screen.getByRole('cell',{name:'amount'})).toBeVisible();
 const size=screen.getByLabelText('Batch size');
 await userEvent.clear(size);await userEvent.type(size,'8');
 expect(screen.getByTestId('canvas-fields')).toHaveTextContent('amount');
 expect(screen.getByRole('cell',{name:'amount'})).toBeVisible();
 expect(container.querySelector('details').open).toBe(true);
});

it('shows a persistent error when the server returns no fields', async()=>{
 api.previewDataLoader.mockResolvedValue({fields:[],preview:[],preview_mode:'declared'});
 const onChange=vi.fn();
 render(<DataLoaderInput config={{loader:'python',code:'def build_dataset(resource): pass'}} onChange={onChange}/>);
 await userEvent.click(screen.getByRole('button',{name:'3. Read output interface'}));
 expect(await screen.findByRole('alert')).toHaveTextContent('No output fields were found');
 expect(onChange).not.toHaveBeenCalled();
 expect(screen.getByRole('button',{name:'3. Read output interface'})).toBeEnabled();
});
