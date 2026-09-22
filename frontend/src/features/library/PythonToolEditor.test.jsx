import React from 'react';
import {render,screen,waitFor,fireEvent} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {beforeEach,describe,it,expect,vi} from 'vitest';
import PythonToolEditor,{CLASS_EXAMPLE,TOOL_EXAMPLE} from './PythonToolEditor.jsx';
import {api} from '../../api.js';
vi.mock('../../api.js',()=>({api:{inspectToolCode:vi.fn(),saveCustomTool:vi.fn(),previewToolCode:vi.fn()}}));

const CLASS_SCHEMA={kind:'class',factory:false,description:'A keyword index.',class:'KeywordIndex',
 needs_discovery:false,external_bases:[],
 configuration:{entrypoint:'KeywordIndex.__init__',inputs:[{name:'min_length',type:'int',default:3,required:false}],warnings:[]},
 tools:[{name:'add',description:'Add a text to the index.',params:[{name:'text',type:'string',description:'the text to index'}]},
        {name:'search',description:'Texts containing a keyword.',params:[{name:'keyword',type:'string',description:''},{name:'limit',type:'integer',required:false,description:''}]}]};

beforeEach(()=>{vi.clearAllMocks();});

it('confirms code without running, separates settings and runtime inputs, and saves',async()=>{
 api.inspectToolCode.mockResolvedValue({kind:'factory',factory:true,configuration:{inputs:[{name:'minimum_length',type:'int',default:1}]},inputs:[{name:'text',type:'str'}],outputs:[{name:'words',type:'int'}]});
 api.previewToolCode.mockResolvedValue({status:'success',result:{words:1}});
 api.saveCustomTool.mockResolvedValue({});
 const saved=vi.fn();render(<PythonToolEditor onClose={vi.fn()} onSaved={saved}/>);
 await userEvent.type(screen.getByLabelText('Tool / toolkit name'),'counter');
 await userEvent.selectOptions(screen.getByLabelText('Example'),'run(inputs) with declared schemas');
 await userEvent.click(screen.getByText('Load example'));
 expect(api.inspectToolCode).not.toHaveBeenCalled();
 await userEvent.click(screen.getByText('Confirm code'));
 const setting=await screen.findByLabelText(/minimum_length/);
 fireEvent.change(setting,{target:{value:'4'}});
 expect(api.previewToolCode).not.toHaveBeenCalled();
 fireEvent.change(screen.getByLabelText('Test inputs (JSON)'),{target:{value:'{"text":"hello world"}'}});
 await userEvent.click(screen.getByText('Run example'));
 await waitFor(()=>expect(api.previewToolCode).toHaveBeenCalledWith(expect.objectContaining({config:{minimum_length:4},args:{text:'hello world'}})));
 await userEvent.click(screen.getByText('Save'));
 await waitFor(()=>expect(api.saveCustomTool).toHaveBeenCalledWith(expect.objectContaining({name:'counter',code:TOOL_EXAMPLE,config:{minimum_length:4}})));
 expect(saved).toHaveBeenCalled();
});

describe('a class is a toolkit',()=>{
 it('lists every method as a tool with its parameters and configuration',async()=>{
  api.inspectToolCode.mockResolvedValue(CLASS_SCHEMA);
  render(<PythonToolEditor onClose={vi.fn()} onSaved={vi.fn()}/>);
  await userEvent.click(screen.getByText('Load example'));
  expect(screen.getByLabelText('Python Tool code')).toHaveValue(CLASS_EXAMPLE);
  await userEvent.click(screen.getByText('Confirm code'));
  const tools=await screen.findByLabelText('Discovered tools');
  expect(tools).toHaveTextContent('add');
  expect(tools).toHaveTextContent('Texts containing a keyword.');
  expect(tools).toHaveTextContent('limit: integer (optional)');
  expect(await screen.findByLabelText(/min_length/)).toBeInTheDocument();
  expect(screen.getByLabelText('Tool interface')).toHaveTextContent('one instance serves every call of a run');
 });

 it('prefills test inputs from the chosen method and shows what it printed',async()=>{
  api.inspectToolCode.mockResolvedValue(CLASS_SCHEMA);
  api.previewToolCode.mockResolvedValue({status:'success',result:['a text'],logs:'indexing 1\n'});
  render(<PythonToolEditor onClose={vi.fn()} onSaved={vi.fn()}/>);
  await userEvent.click(screen.getByText('Load example'));
  await userEvent.click(screen.getByText('Confirm code'));
  await waitFor(()=>expect(screen.getByLabelText('Test inputs (JSON)')).toHaveValue('{\n  "text": ""\n}'));
  await userEvent.selectOptions(screen.getByLabelText('Test tool'),'search');
  expect(screen.getByLabelText('Test inputs (JSON)')).toHaveValue('{\n  "keyword": "",\n  "limit": 0\n}');
  await userEvent.click(screen.getByText('Run example'));
  await waitFor(()=>expect(api.previewToolCode).toHaveBeenCalledWith(expect.objectContaining({tool:'search',args:{keyword:'',limit:0}})));
  expect(await screen.findByText(/indexing 1/)).toBeInTheDocument();
 });

 it('reads an imported class only when asked to import the module',async()=>{
  api.inspectToolCode.mockResolvedValueOnce({...CLASS_SCHEMA,class:null,tools:[],needs_discovery:true,
    configuration:{entrypoint:'TOOL_CLASS()',inputs:[],warnings:[]}});
  render(<PythonToolEditor onClose={vi.fn()} onSaved={vi.fn()}/>);
  await userEvent.click(screen.getByText('Load example'));
  await userEvent.click(screen.getByText('Confirm code'));
  expect(await screen.findByText(/Read tools by importing/)).toBeInTheDocument();
  api.inspectToolCode.mockResolvedValueOnce(CLASS_SCHEMA);
  await userEvent.click(screen.getByText('Read tools by importing'));
  await waitFor(()=>expect(api.inspectToolCode).toHaveBeenLastCalledWith(CLASS_EXAMPLE,expect.objectContaining({execute:true})));
  expect(await screen.findByLabelText('Discovered tools')).toHaveTextContent('search');
 });

 it('shows a failing call with the line it names',async()=>{
  api.inspectToolCode.mockResolvedValue(CLASS_SCHEMA);
  api.previewToolCode.mockRejectedValue({body:{detail:'Tool code line 12: ZeroDivisionError: division by zero\nIn your code:\n  line 12, in add: 1/0'}});
  render(<PythonToolEditor onClose={vi.fn()} onSaved={vi.fn()}/>);
  await userEvent.click(screen.getByText('Load example'));
  await userEvent.click(screen.getByText('Confirm code'));
  await userEvent.click(await screen.findByText('Run example'));
  expect(await screen.findByRole('alert')).toHaveTextContent('Tool code line 12');
 });
});

describe('an uploaded folder toolkit',()=>{
 it("edits the entry file and saves it back into the folder",async()=>{
  api.inspectToolCode.mockResolvedValue(CLASS_SCHEMA);
  api.saveCustomTool.mockResolvedValue({});
  const initial={name:'mylib',code:'TOOL_CLASS = Client\n',kind:'class',config:{min_length:4},
    package:{entry:'mylib/__init__.py',files:4,requirements:[]},tools:CLASS_SCHEMA.tools};
  render(<PythonToolEditor initial={initial} onClose={vi.fn()} onSaved={vi.fn()}/>);
  expect(screen.getByLabelText('Tool / toolkit name')).toBeDisabled();
  expect(screen.getByRole('note')).toHaveTextContent('mylib/__init__.py');
  await waitFor(()=>expect(api.inspectToolCode).toHaveBeenCalledWith('TOOL_CLASS = Client\n',{name:'mylib'}));
  fireEvent.change(screen.getByLabelText('Python Tool code'),{target:{value:'TOOL_CLASS = Other\n'}});
  await userEvent.click(screen.getByText('Confirm code'));
  await userEvent.click(await screen.findByText('Save'));
  await waitFor(()=>expect(api.saveCustomTool).toHaveBeenCalledWith(expect.objectContaining(
    {name:'mylib',code:'TOOL_CLASS = Other\n',edit_entry:true})));
 });
});
