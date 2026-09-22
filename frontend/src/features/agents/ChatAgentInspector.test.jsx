import React from 'react';
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
it('defaults to all tools and saves individual tools plus attached skills', async () => {
  const agent={id:'a',name:'Chat',instructions:'Help',max_steps:20,timeout:300,memories:[]};
  const save=vi.fn(async (_,patch) => ({...agent,...patch}));
  const {container}=render(<ChatAgentInspector graphId="g" agent={agent} onBack={vi.fn()} onSave={save} />);
  const view=within(container), user=userEvent.setup();
  await user.click(view.getByRole('button',{name:'Harness settings →'}));
  expect(view.getByLabelText('Tool access')).toHaveValue('all');
  await user.selectOptions(view.getByLabelText('Tool access'),'selected');
  await user.click(await view.findByLabelText('sum'));
  await user.click(view.getByLabelText(/analysis/));
  await user.click(view.getByRole('button',{name:'Save settings'}));
  await waitFor(()=>expect(save).toHaveBeenCalledWith(agent,expect.objectContaining({tools:['double'],toolkits:null,skill_names:['analysis']})));
  await user.click(view.getByRole('button',{name:'Harness settings →'}));
  expect(view.getByLabelText('double')).toBeChecked();
  expect(view.getByLabelText('sum')).not.toBeChecked();
  expect(view.getByLabelText(/analysis/)).toBeChecked();
  await user.selectOptions(view.getByLabelText('Tool access'),'all');
  await user.click(view.getByRole('button',{name:'Save settings'}));
  await waitFor(()=>expect(save).toHaveBeenLastCalledWith(agent,expect.objectContaining({tools:null})));
});

it('returns to the running conversation when the Agent is reopened and lets messages be copied', async () => {
  const {api} = await import('../../api.js');
  const agent={id:'a2',name:'Researcher',instructions:'Help',max_steps:20,timeout:300,memories:[]};
  const newer={id:'s-new',created_at:2,status:'idle',messages:[],events:[]};
  const running={id:'s-run',created_at:1,status:'running',messages:[{role:'user',content:'Investigate the outage'}],events:[]};
  api.agentSessions.mockResolvedValue({sessions:[newer, running]});
  const first=render(<ChatAgentInspector graphId="g" agent={agent} onBack={vi.fn()} onSave={vi.fn()} />);
  expect(await within(first.container).findByText('Agent is running…')).toBeInTheDocument();
  first.unmount();   // the user opened another node
  const {container}=render(<ChatAgentInspector graphId="g" agent={agent} onBack={vi.fn()} onSave={vi.fn()} />);
  const view=within(container);
  expect(await view.findByText('Investigate the outage')).toBeInTheDocument();
  expect(view.getByText('Agent is running…')).toBeInTheDocument();
  const user=userEvent.setup();
  await user.click(view.getByRole('button',{name:'Copy your message'}));
  expect(await navigator.clipboard.readText()).toBe('Investigate the outage');
});

it('says what Stop reached and what it cannot recall, and never offers Stop twice', async () => {
  const {api} = await import('../../api.js');
  const agent={id:'a3',name:'Runner',instructions:'Help',max_steps:20,timeout:300,memories:[]};
  const note='Stopping. The Agent stops at its next step; a model or tool request already sent cannot be recalled and finishes on the server.';
  const running={id:'s-stop',created_at:1,status:'running',messages:[{role:'user',content:'Do the long thing'}],events:[]};
  api.agentSessions.mockResolvedValue({sessions:[running]});
  api.stopAgentSession=vi.fn(async()=>({...running,status:'stopping',stop_note:note}));
  const {container}=render(<ChatAgentInspector graphId="g" agent={agent} onBack={vi.fn()} onSave={vi.fn()} />);
  const view=within(container), user=userEvent.setup();
  expect(await view.findByText('Agent is running…')).toBeInTheDocument();
  // The list keeps reporting it as stopping, as the server does until it settles.
  api.agentSessions.mockResolvedValue({sessions:[{...running,status:'stopping',stop_note:note}]});
  await user.click(view.getByRole('button',{name:'Stop'}));
  expect(await view.findByText(note)).toBeInTheDocument();
  expect(view.getByRole('button',{name:'Stop'})).toBeDisabled();

  // It settled: the note goes with it and the conversation is usable again.
  api.agentSessions.mockResolvedValue({sessions:[{...running,status:'stopped',error:'Execution stopped. You can continue in this conversation.'}]});
  await waitFor(()=>expect(view.queryByText(note)).not.toBeInTheDocument(),{timeout:5000});
  expect(view.queryByRole('button',{name:'Stop'})).toBeNull();
  expect(view.getByRole('button',{name:'Send'})).toBeDisabled();  // no message typed yet
});
