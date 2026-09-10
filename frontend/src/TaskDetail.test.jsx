import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import TaskDetail from './TaskDetail.jsx';
import { api } from './api.js';
vi.mock('./api.js',()=>({api:{getGraph:vi.fn(),listResultRuns:vi.fn(),runPlan:vi.fn(),getRun:vi.fn()}}));
beforeEach(()=>{
 vi.resetAllMocks();
 vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
 window.matchMedia = vi.fn(() => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
 api.getGraph.mockResolvedValue({id:'g',name:'Summarize',goal:'Summarize feedback',tasks:[{tool_names:['Search'],skill_names:[]}]});
 api.listResultRuns.mockResolvedValue([]);
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
it('browses multiple records, filters batches and exposes step outputs',async()=>{
 api.listResultRuns.mockResolvedValue([
  {run_id:'r1',status:'success',batch_id:'b1',nodes:[{name:'feed',output:{company:'Alpha'}}]},
  {run_id:'r2',status:'success',batch_id:'b1',nodes:[{name:'feed',output:{company:'Beta'}}]},
  {run_id:'r3',status:'failed',batch_id:null},
 ]);
 api.getRun.mockImplementation(async id=>({run_id:id,status:'success',result:`Output ${id}`,inputs:{input:'Source data'},nodes:[{name:'analyze',status:'completed',output:'Analysis details'}]}));
 render(<TaskDetail graphId="g"/>);
 fireEvent.click((await screen.findAllByText('View result →'))[0]);
 expect(await screen.findByText('Output r1')).toBeInTheDocument();
 fireEvent.change(screen.getByLabelText('Filter batch'),{target:{value:'b1'}});
 expect(screen.getByText('(2 / 3)')).toBeInTheDocument();
 fireEvent.click(screen.getByRole('button',{name:/Beta/}));
 expect(await screen.findByText('Output r2')).toBeInTheDocument();
 expect(screen.queryByText('Output r1')).not.toBeInTheDocument();
 expect(screen.getByText('analyze · completed')).toBeInTheDocument();
 fireEvent.click(screen.getByText('analyze · completed'));
 expect(screen.getByText('Analysis details')).toBeVisible();
 fireEvent.change(screen.getByLabelText('Search run records'),{target:{value:'missing'}});
 expect(screen.getByText('No matching records.')).toBeInTheDocument();
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
