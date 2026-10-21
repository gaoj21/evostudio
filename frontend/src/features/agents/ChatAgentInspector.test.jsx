import React, { useState } from 'react';
import {render, within, waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {it, expect, vi} from 'vitest';
import ChatAgentInspector from './ChatAgentInspector.jsx';
vi.mock('../../api.js', () => ({api: {
  listTools: vi.fn(async () => ({tools:[{name:'Math', available:true, tools:[{name:'double',description:'Multiply by two'},{name:'sum',description:'Add numbers'}]}]})),
  listSkills: vi.fn(async () => ({skills:[{name:'analysis',description:'Analyze evidence'}]})),
  agentSessions: vi.fn(async () => ({sessions:[]})),
  chatMemoryResources: vi.fn(async () => ({resources:[]})),
}}));

// The node's data as the canvas holds it; `saved` is the same node as last saved.
const node = (extra = {}) => ({ name: 'chat', kind: 'chat_agent', agent_id: 'a', description: '', instructions: 'Help',
  provider: null, max_steps: 20, timeout: 300, toolkits: null, tools: null, skill_names: [], memories: [], ...extra });

// The inspector as App drives it: every edit goes into the node.
function Host({ initial, saved = initial, onChange = () => {}, ...props }) {
  const [agent, setAgent] = useState(initial);
  return <ChatAgentInspector graphId="g" agent={agent} saved={saved} onBack={vi.fn()}
    onChange={patch => { onChange(patch); setAgent(a => ({ ...a, ...patch })); }}
    onRename={name => setAgent(a => ({ ...a, name }))} {...props} />;
}

it('defaults to all tools and writes individual tools and skills into the node', async () => {
  const change = vi.fn();
  const {container}=render(<Host initial={node()} onChange={change} />);
  const view=within(container), user=userEvent.setup();
  await user.click(view.getByRole('button',{name:'Harness settings →'}));
  expect(view.getByLabelText('Tool access')).toHaveValue('all');
  await user.selectOptions(view.getByLabelText('Tool access'),'selected');
  await user.click(await view.findByLabelText('sum'));
  await user.click(view.getByLabelText(/analysis/));
  expect(change).toHaveBeenCalledWith({ tools: ['double'] });
  expect(change).toHaveBeenLastCalledWith({ skill_names: ['analysis'] });
  expect(view.getByLabelText('double')).toBeChecked();
  expect(view.getByLabelText('sum')).not.toBeChecked();
  expect(view.getByLabelText(/analysis/)).toBeChecked();
  expect(view.getByText('Unsaved settings: messages use the saved settings until you save the workflow.')).toBeInTheDocument();
  await user.selectOptions(view.getByLabelText('Tool access'),'all');
  expect(change).toHaveBeenLastCalledWith({ tools: null });
});

it('renames the node on Enter and keeps its conversations (agent_id)', async () => {
  const rename = vi.fn();
  const {container}=render(<ChatAgentInspector graphId="g" agent={node()} saved={node()} onBack={vi.fn()} onChange={vi.fn()} onRename={rename} />);
  const view=within(container), user=userEvent.setup();
  await user.click(view.getByRole('button',{name:'Harness settings →'}));
  await user.clear(view.getByLabelText('Name'));
  await user.type(view.getByLabelText('Name'),'Researcher{Enter}');
  expect(rename).toHaveBeenCalledWith('Researcher');
  // A rename is not an unsaved setting: the conversation is the Agent's.
  expect(view.queryByText(/Unsaved settings/)).toBeNull();
});

it('cannot chat with an Agent that has never been saved, and offers to save', async () => {
  const save = vi.fn();
  const {container}=render(<ChatAgentInspector graphId="g" agent={node()} saved={null} onBack={vi.fn()} onChange={vi.fn()} onSaveWorkflow={save} />);
  const view=within(container), user=userEvent.setup();
  expect(view.getByText('This Chat Agent is not saved yet. Save the workflow to chat with it.')).toBeInTheDocument();
  await user.type(view.getByLabelText('Message Chat Agent'),'hello');
  expect(view.getByRole('button',{name:'Send'})).toBeDisabled();
  expect(view.getByRole('button',{name:'New session'})).toBeDisabled();
  await user.click(view.getByRole('button',{name:'Save workflow'}));
  expect(save).toHaveBeenCalled();
});

it('returns to the running conversation when the Agent is reopened and lets messages be copied', async () => {
  const {api} = await import('../../api.js');
  const agent=node({agent_id:'a2',name:'researcher'});
  const newer={id:'s-new',created_at:2,status:'idle',messages:[],events:[]};
  const running={id:'s-run',created_at:1,status:'running',messages:[{role:'user',content:'Investigate the outage'}],events:[]};
  api.agentSessions.mockResolvedValue({sessions:[newer, running]});
  const first=render(<ChatAgentInspector graphId="g" agent={agent} saved={agent} onBack={vi.fn()} onChange={vi.fn()} />);
  expect(await within(first.container).findByText('Agent is running…')).toBeInTheDocument();
  expect(api.agentSessions).toHaveBeenCalledWith('g', 'a2');
  first.unmount();   // the user opened another node
  const {container}=render(<ChatAgentInspector graphId="g" agent={agent} saved={agent} onBack={vi.fn()} onChange={vi.fn()} />);
  const view=within(container);
  expect(await view.findByText('Investigate the outage')).toBeInTheDocument();
  expect(view.getByText('Agent is running…')).toBeInTheDocument();
  const user=userEvent.setup();
  await user.click(view.getByRole('button',{name:'Copy your message'}));
  expect(await navigator.clipboard.readText()).toBe('Investigate the outage');
});

it('says what Stop reached and what it cannot recall, and never offers Stop twice', async () => {
  const {api} = await import('../../api.js');
  const agent=node({agent_id:'a3',name:'runner'});
  const note='Stopping. The Agent stops at its next step; a model or tool request already sent cannot be recalled and finishes on the server.';
  const running={id:'s-stop',created_at:1,status:'running',messages:[{role:'user',content:'Do the long thing'}],events:[]};
  api.agentSessions.mockResolvedValue({sessions:[running]});
  api.stopAgentSession=vi.fn(async()=>({...running,status:'stopping',stop_note:note}));
  const {container}=render(<ChatAgentInspector graphId="g" agent={agent} saved={agent} onBack={vi.fn()} onChange={vi.fn()} />);
  const view=within(container), user=userEvent.setup();
  expect(await view.findByText('Agent is running…')).toBeInTheDocument();
  // The list keeps reporting it as stopping, as the server does until it settles.
  api.agentSessions.mockResolvedValue({sessions:[{...running,status:'stopping',stop_note:note}]});
  await user.click(view.getByRole('button',{name:'Stop'}));
  expect(api.stopAgentSession).toHaveBeenCalledWith('g','a3','s-stop');
  expect(await view.findByText(note)).toBeInTheDocument();
  expect(view.getByRole('button',{name:'Stop'})).toBeDisabled();

  // It settled: the note goes with it and the conversation is usable again.
  api.agentSessions.mockResolvedValue({sessions:[{...running,status:'stopped',error:'Execution stopped. You can continue in this conversation.'}]});
  await waitFor(()=>expect(view.queryByText(note)).not.toBeInTheDocument(),{timeout:5000});
  expect(view.queryByRole('button',{name:'Stop'})).toBeNull();
  expect(view.getByRole('button',{name:'Send'})).toBeDisabled();  // no message typed yet
});

it('leaves without asking: unsaved settings are the workflow\'s, kept on the canvas', async () => {
  const onBack=vi.fn();
  const ask=vi.spyOn(window,'confirm');
  const {container}=render(<Host initial={node()} onBack={onBack} />);
  const view=within(container), user=userEvent.setup();
  await user.click(view.getByRole('button',{name:'Harness settings →'}));
  await user.type(view.getByLabelText('Instructions'),' more');
  expect(view.getByText(/Unsaved settings/)).toBeInTheDocument();
  await user.click(view.getByRole('button',{name:'← Back to chat'}));
  await user.click(view.getByRole('button',{name:'← Back to workflow'}));
  expect(ask).not.toHaveBeenCalled();
  expect(onBack).toHaveBeenCalled();
  ask.mockRestore();
});

it('shows a connection this task can no longer reach and removes it from the node', async () => {
  const {api} = await import('../../api.js');
  const gone='b'.repeat(32), live='c'.repeat(32);
  api.agentSessions.mockResolvedValue({sessions:[]});
  api.chatMemoryResources.mockResolvedValueOnce({resources:[{id:`mem:space:${live}`,name:'Live notes',kind:'mem0',space_id:live,writable:true}]});
  const agent=node({agent_id:'stale',name:'helper',memories:[{space_id:gone,read:true,write:true},{space_id:live,read:true,write:false}]});
  const change=vi.fn();
  const {container}=render(<Host initial={agent} onChange={change} />);
  const view=within(container), user=userEvent.setup();
  expect(await view.findByText(/unavailable \(remove it in Harness settings\)/)).toHaveTextContent(`Shared memory ${gone.slice(0,8)}`);
  await user.click(view.getByRole('button',{name:'Harness settings →'}));
  const group=view.getByRole('group',{name:'Unavailable memories'});
  expect(group).toHaveTextContent(`Shared memory ${gone.slice(0,8)} · Read / Write`);
  expect(group).not.toHaveTextContent('Live notes');
  await user.click(view.getByRole('button',{name:'Remove unavailable memories'}));
  expect(change).toHaveBeenLastCalledWith({memories:[{space_id:live,read:true,write:false}]});
  expect(view.queryByRole('group',{name:'Unavailable memories'})).toBeNull();
});
