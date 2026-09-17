import React from 'react';
import {render,screen,waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {it,expect,vi,beforeEach} from 'vitest';
import EvaluatorInspector from './EvaluatorInspector.jsx';
import {api} from '../../api.js';
import {graphToFlow,flowToGraph} from '../canvas/convert.js';
it('persists evaluator config and mapped branch through canvas conversion',()=>{
 const graph={id:'test',tasks:[{name:'evaluate',kind:'evaluator',evaluator:{type:'tool',timing:'batch'},inputs:[{name:'prediction',type:'any'}],outputs:[],x:10,y:20}],edges:[]};
 const flow=graphToFlow(graph);expect(flow.nodes[0].type).toBe('evaluator');
 const saved=flowToGraph(graph,flow.nodes,flow.edges);expect(saved.tasks[0].kind).toBe('evaluator');expect(saved.tasks[0].evaluator).toEqual(graph.tasks[0].evaluator);
});
vi.mock('../../api.js',()=>({api:{listDataResources:vi.fn(),listBatches:vi.fn(),listRuns:vi.fn(),inspectEvaluatorCode:vi.fn(),previewEvaluatorCode:vi.fn()}}));
beforeEach(()=>{api.listDataResources.mockResolvedValue({resources:[]});});
it('replacing a legacy evaluator preserves independent evaluation timing',async()=>{
 const update=vi.fn();render(<EvaluatorInspector node={{id:'score',data:{evaluator:{type:'exact_match',timing:'run'}}}} onUpdate={update} onRename={vi.fn()}/>);
 await userEvent.click(screen.getByText('Replace with Python evaluator'));
 expect(update).toHaveBeenCalledWith('score',{evaluator:expect.objectContaining({type:'python',timing:'run',code:expect.stringContaining('def evaluate')})});
});

it('uploads Python, generates parameters and previews saved results without mutating the graph',async()=>{
 api.listBatches.mockResolvedValue([{batch_id:'saved-batch',status:'succeeded',total:2}]);
 api.listRuns.mockResolvedValue([]);
 api.inspectEvaluatorCode.mockResolvedValue({inputs:[{name:'threshold',type:'float',default:.5}],warnings:[]});
 api.previewEvaluatorCode.mockResolvedValue({execution_count:2,metrics:[{name:'accuracy',type:'number',sample:1}],report:{metrics:{accuracy:1},objective:{metric:'accuracy'}}});
 const graph={id:'test',tasks:[{name:'quality',kind:'evaluator',evaluator:{type:'python'}}],edges:[]};
 const initial=JSON.stringify(graph),update=vi.fn();
 function Wrapper(){
  const [data,setData]=React.useState({evaluator:{type:'python',code:''}});
  return <EvaluatorInspector node={{id:'quality',data}} getGraph={()=>graph} onRename={vi.fn()} onUpdate={(id,patch)=>{update(id,patch);setData(d=>({...d,...patch}));}}/>;
 }
 render(<Wrapper/>);
 const code='def evaluate(records, threshold: float = 0.5):\n    return {"metrics": {"accuracy": 1}}';
 await userEvent.upload(screen.getByLabelText('Upload Python Evaluator'),new File([code],'evaluate.py',{type:'text/x-python'}));
 await waitFor(()=>expect(screen.getByLabelText('Python Evaluator code')).toHaveValue(code));
 expect(api.inspectEvaluatorCode).not.toHaveBeenCalled();
 await userEvent.click(screen.getByText('Confirm code'));
 expect(await screen.findByLabelText(/threshold/)).toHaveValue(.5);
 await userEvent.click(screen.getByText('Preview evaluation interface'));
 expect(await screen.findByLabelText('Evolve objective')).toHaveValue('accuracy');
 expect(api.previewEvaluatorCode).toHaveBeenCalledWith('test',expect.objectContaining({batch_id:'saved-batch',evaluator:'quality'}));
 await waitFor(()=>expect(update).toHaveBeenLastCalledWith('quality',expect.objectContaining({evaluator:expect.objectContaining({code,metric:'accuracy'})})));
 expect(JSON.stringify(graph)).toBe(initial);
});
