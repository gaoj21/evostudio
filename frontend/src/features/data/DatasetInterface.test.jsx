import React from 'react';
import {render,screen,waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {it,expect,vi} from 'vitest';
import DatasetInterface,{DatasetOutputs} from './DatasetInterface.jsx';
import {api} from '../../api.js';
vi.mock('../../api.js',()=>({api:{inspectDataLoaderCode:vi.fn()}}));
it('generates typed form fields and exposes inferred outputs',async()=>{
 api.inspectDataLoaderCode.mockResolvedValue({inputs:[{name:'count',type:'int',required:true},{name:'split',type:'str',default:'dev',options:['dev','test']}],warnings:[]});
 const changed=vi.fn(), schema=vi.fn();
 render(<DatasetInterface code="code" values={{}} onChange={changed} onSchema={schema}/>);
 const count=await screen.findByLabelText(/count/);
 await userEvent.type(count,'3');
 expect(changed).toHaveBeenCalledWith({count:3});
 await userEvent.selectOptions(screen.getByLabelText(/split/),'"test"');
 expect(changed).toHaveBeenCalledWith({split:'test'});
 await waitFor(()=>expect(schema).toHaveBeenCalled());
 render(<DatasetOutputs fields={[{name:'news',type:'str',nullable:false,sample:'hello'}]}/>);
 expect(screen.getByRole('cell',{name:'news'})).toBeTruthy();
 expect(screen.getByRole('cell',{name:'"hello"'})).toBeTruthy();
});

it('names packages and models that are not installed, with the install command',async()=>{
 api.inspectDataLoaderCode.mockResolvedValue({inputs:[],warnings:[],dependencies:[
  {name:'spacy',kind:'package',available:true,install:'/venv/bin/python -m pip install spacy'},
  {name:'redactor',kind:'package',available:false,install:'/venv/bin/python -m pip install redactor'},
  {name:'en_core_web_sm',kind:'spacy model',available:false,install:'/venv/bin/python -m spacy download en_core_web_sm'}]});
 render(<DatasetInterface code="import spacy" values={{}} onChange={vi.fn()} onSchema={vi.fn()}/>);
 const missing=await screen.findByTestId('missing-dependencies');
 expect(missing).toHaveTextContent('redactor');
 expect(missing).toHaveTextContent('pip install redactor');
 expect(missing).toHaveTextContent('spacy download en_core_web_sm');
 expect(missing).not.toHaveTextContent('pip install spacy');
 expect(screen.getByText(/Uses installed: spacy/)).toBeTruthy();
});
