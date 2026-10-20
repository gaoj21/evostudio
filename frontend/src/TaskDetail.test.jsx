import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import TaskDetail from './TaskDetail.jsx';
import { api } from './api.js';
vi.mock('./api.js',()=>({api:{getGraph:vi.fn(),listResultRuns:vi.fn(),listBatches:vi.fn(),runPlan:vi.fn(),getRun:vi.fn()}}));
beforeEach(()=>{
 vi.resetAllMocks();
 vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
 window.matchMedia = vi.fn(() => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
 api.getGraph.mockResolvedValue({id:'g',name:'Summarize',goal:'Summarize feedback',tasks:[{tool_names:['Search'],skill_names:[]}]});
 api.listResultRuns.mockResolvedValue([]);
 api.listBatches.mockResolvedValue([]);
 api.runPlan.mockResolvedValue({inputs:[{name:'input',description:'Customer comments',type:'str',required:true}]});
});
it('shows inputs and requires an explicit preparation action',async()=>{
 const run=vi.fn();render(<TaskDetail graphId="g" onRun={run}/>);
 expect(await screen.findByText('Customer comments')).toBeInTheDocument();
 expect(screen.getByText('Search')).toBeInTheDocument();
 expect(run).not.toHaveBeenCalled();
 fireEvent.click(screen.getByText('Prepare run'));expect(run).toHaveBeenCalledTimes(1);
});
it('keeps invalid workflows editable but cannot launch',async()=>{
 api.runPlan.mockRejectedValue(new Error('invalid'));
 const edit=vi.fn();render(<TaskDetail graphId="g" onEdit={edit}/>);
 expect(await screen.findByRole('alert')).toHaveTextContent('needs configuration');
 expect(screen.getByText('Prepare run')).toBeDisabled();
 fireEvent.click(screen.getByText('Configure task'));expect(edit).toHaveBeenCalled();
});
it('loads a selected historical run without rerunning it',async()=>{
 api.listResultRuns.mockResolvedValue([{run_id:'r',status:'success',created_at:'2026-09-09T00:00:00Z'}]);
 api.getRun.mockResolvedValue({status:'success',result:'Reviewed result'});
 render(<TaskDetail graphId="g"/>);
 fireEvent.click(await screen.findByText('View result →'));
 await waitFor(()=>expect(api.getRun).toHaveBeenCalledWith('r'));
 expect(await screen.findByText('Reviewed result')).toBeInTheDocument();
});
it('opens a separate page and restores the run list position and focus',async()=>{
 api.listResultRuns.mockResolvedValue([{run_id:'r',status:'success'}]);
 api.getRun.mockResolvedValue({status:'success',result:'Reviewed result'});
 render(<TaskDetail graphId="g"/>);
 const trigger=await screen.findByText('View result →');
 screen.getByRole('main').scrollTop=640;
 fireEvent.click(trigger);
 expect(screen.queryByText('Recent runs')).not.toBeInTheDocument();
 expect(screen.getByRole('main').scrollTop).toBe(0);
 expect(await screen.findByText('Reviewed result')).toBeInTheDocument();
 fireEvent.click(screen.getByRole('button',{name:'← Back to task'}));
 expect(screen.getByText('Recent runs')).toBeInTheDocument();
 expect(screen.queryByText('Reviewed result')).not.toBeInTheDocument();
 expect(screen.getByRole('main').scrollTop).toBe(640);
 expect(screen.getByText('View result →').closest('button')).toHaveFocus();
});
it('can return while loading without a late response reopening the result',async()=>{
 let resolve;
 api.listResultRuns.mockResolvedValue([{run_id:'r',status:'success'}]);
 api.getRun.mockImplementation(()=>new Promise(done=>{resolve=done;}));
 render(<TaskDetail graphId="g"/>);
 fireEvent.click(await screen.findByText('View result →'));
 expect(screen.getByText('Loading result…')).toBeInTheDocument();
 fireEvent.click(screen.getByRole('button',{name:'← Back to task'}));
 resolve({status:'success',result:'Late result'});
 await waitFor(()=>expect(screen.getByText('Recent runs')).toBeInTheDocument());
 expect(screen.queryByText('Late result')).not.toBeInTheDocument();
});
it('retries failed result loading within the detail page',async()=>{
 api.listResultRuns.mockResolvedValue([{run_id:'r',status:'success'}]);
 api.getRun.mockRejectedValueOnce(new Error('Result unavailable')).mockResolvedValueOnce({status:'success',result:'Recovered result'});
 render(<TaskDetail graphId="g"/>);
 fireEvent.click(await screen.findByText('View result →'));
 expect(await screen.findByRole('alert')).toHaveTextContent('Result unavailable');
 expect(screen.getByRole('button',{name:'← Back to task'})).toBeEnabled();
 fireEvent.click(screen.getByText('Retry'));
 expect(await screen.findByText('Recovered result')).toBeInTheDocument();
});
// The server's paging, filtering and search over `all`, as the results endpoint does it.
const serve=all=>async(graphId,page={})=>{
 const q=(page.q||'').toLowerCase();
 const matched=all.filter(r=>(!page.batch||(page.batch==='single'?!r.batch_id:r.batch_id===page.batch))&&(!q||JSON.stringify(r).toLowerCase().includes(q)));
 const offset=page.offset||0,limit=page.limit||50;
 const runs=matched.slice(offset,offset+limit);
 return {runs,total:matched.length,has_more:offset+runs.length<matched.length};
};
it('browses multiple records, filters batches and exposes step outputs',async()=>{
 api.listResultRuns.mockImplementation(serve([
  {run_id:'r1',status:'success',batch_id:'b1',nodes:[{name:'reader',kind:'source',output:{customer:'Alpha',long:'x'.repeat(200)}}]},
  {run_id:'r2',status:'success',batch_id:'b1',nodes:[{name:'reader',kind:'source',output:{customer:'Beta'}}]},
  {run_id:'r3',status:'failed',batch_id:null},
 ]));
 api.listBatches.mockResolvedValue([{batch_id:'b1',status:'success',total:2}]);
 api.getRun.mockImplementation(async id=>({run_id:id,status:'success',result:`Output ${id}`,inputs:{input:'Source data'},nodes:[{name:'analyze',status:'completed',output:'Analysis details'}]}));
 render(<TaskDetail graphId="g"/>);
 fireEvent.click((await screen.findAllByText('View result →'))[0]);
 expect(await screen.findByText('Output r1')).toBeInTheDocument();
 expect(screen.getByText('(3 / 3)')).toBeInTheDocument();
 fireEvent.change(screen.getByLabelText('Filter batch'),{target:{value:'b1'}});
 expect(await screen.findByText('(2 / 2)')).toBeInTheDocument();
 expect(api.listResultRuns).toHaveBeenLastCalledWith('g',expect.objectContaining({batch:'b1',offset:0}));
 fireEvent.click(screen.getByRole('button',{name:/Beta/}));
 expect(await screen.findByText('Output r2')).toBeInTheDocument();
 expect(screen.queryByText('Output r1')).not.toBeInTheDocument();
 expect(screen.getByText('analyze · completed')).toBeInTheDocument();
 fireEvent.click(screen.getByText('analyze · completed'));
 expect(screen.getByText('Analysis details')).toBeVisible();
 fireEvent.change(screen.getByLabelText('Search run records'),{target:{value:'missing'}});
 expect(await screen.findByText('No matching records.')).toBeInTheDocument();
 expect(api.listResultRuns).toHaveBeenLastCalledWith('g',expect.objectContaining({batch:'b1',q:'missing'}));
});
it('shows the task before its run history arrives',async()=>{
 api.listResultRuns.mockImplementation(()=>new Promise(()=>{}));
 api.listBatches.mockImplementation(()=>new Promise(()=>{}));
 render(<TaskDetail graphId="g"/>);
 expect(await screen.findByRole('heading',{name:'Summarize'})).toBeInTheDocument();
 expect(screen.getByText('Summarize feedback')).toBeInTheDocument();
 expect(screen.getByText('Loading runs…')).toBeInTheDocument();
 expect(screen.getByText('Loading batch runs…')).toBeInTheDocument();
 expect(screen.queryByText('Loading task…')).not.toBeInTheDocument();
 expect(api.listResultRuns).toHaveBeenCalledWith('g',{limit:50});
});
it('shows a failed run history in its own section',async()=>{
 api.listResultRuns.mockRejectedValue(new Error('History unavailable'));
 render(<TaskDetail graphId="g"/>);
 expect(await screen.findByRole('alert')).toHaveTextContent('History unavailable');
 expect(screen.getByRole('heading',{name:'Summarize'})).toBeInTheDocument();
 expect(screen.getByText('Customer comments')).toBeInTheDocument();
});
it('loads more runs and searches all of them on the server',async()=>{
 const all=Array.from({length:120},(_,i)=>({run_id:`r${i}`,status:'success',inputs:{customer:i===110?'Zeta':`C${i}`}}));
 api.listResultRuns.mockImplementation(serve(all));
 api.getRun.mockImplementation(async id=>({run_id:id,status:'success',result:`Output ${id}`}));
 render(<TaskDetail graphId="g"/>);
 expect(await screen.findByText('Showing the newest 50 of 120 runs. Open one to browse and search them all.')).toBeInTheDocument();
 fireEvent.click(screen.getAllByText('View result →')[0]);
 expect(await screen.findByText('(50 / 120)')).toBeInTheDocument();
 fireEvent.click(screen.getByRole('button',{name:'Load more'}));
 expect(await screen.findByText('(100 / 120)')).toBeInTheDocument();
 expect(api.listResultRuns).toHaveBeenLastCalledWith('g',expect.objectContaining({offset:50,limit:50}));
 fireEvent.click(screen.getByRole('button',{name:'Load more'}));
 expect(await screen.findByText('(120 / 120)')).toBeInTheDocument();
 expect(screen.queryByRole('button',{name:'Load more'})).not.toBeInTheDocument();
 // Not among the first page, found by the server all the same.
 fireEvent.change(screen.getByLabelText('Search run records'),{target:{value:'zeta'}});
 expect(await screen.findByText('(1 / 1)')).toBeInTheDocument();
 expect(screen.getByRole('button',{name:/Zeta/})).toBeInTheDocument();
 expect(api.listResultRuns).toHaveBeenLastCalledWith('g',expect.objectContaining({q:'zeta',offset:0}));
});
it('names runs by the trajectory fields the Input type declares',async()=>{
 api.getGraph.mockResolvedValue({id:'g',name:'T',tasks:[{name:'reader',kind:'source',source:{type:'project_feed'}}]});
 api.listSourceTypes=vi.fn().mockResolvedValue({source_types:[{type:'project_feed',sequence:{group:'ticket',order:'day'}}]});
 api.listResultRuns.mockResolvedValue([
  {run_id:'r1',status:'success',nodes:[{name:'reader',output:{body:'first text',ticket:'T-1',day:'2026-01-02'}}]},
 ]);
 api.getRun.mockResolvedValue({run_id:'r1',status:'success',result:'Done'});
 render(<TaskDetail graphId="g"/>);
 fireEvent.click(await screen.findByText('View result →'));
 expect(await screen.findByRole('button',{name:/T-1 · 2026-01-02/})).toBeInTheDocument();
 delete api.listSourceTypes;
});
it('collapses input data as a whole and provides a keyboard-accessible resize separator',async()=>{
 api.listResultRuns.mockResolvedValue([{run_id:'r',status:'success'}]);
 api.getRun.mockResolvedValue({status:'success',inputs:{original:'Original input'},result:'Done'});
 render(<TaskDetail graphId="g"/>);
 fireEvent.click(await screen.findByText('View result →'));
 const input=await screen.findByText('Input data');
 const details=input.closest('details');
 expect(details.open).toBe(true);
 fireEvent.click(input);
 expect(details.open).toBe(false);
 expect(screen.getByText('Final output')).toBeVisible();
 fireEvent.click(input);
 expect(details.open).toBe(true);
 const separator=screen.getByRole('separator',{name:'Resize result and chat panels'});
 expect(separator).toHaveAttribute('tabindex','0');
});

it('keeps an interrupted batch discoverable even before its first result',async()=>{
 api.listBatches.mockResolvedValue([{batch_id:'b0',status:'interrupted',total:0}]);
 const open=vi.fn();
 render(<TaskDetail graphId="g" onOpenBatch={open}/>);
 expect(await screen.findByText('interrupted')).toBeVisible();
 fireEvent.click(screen.getByText('Open / Resume →'));
 expect(open).toHaveBeenCalledWith('b0');
 expect(api.getRun).not.toHaveBeenCalled();
});
