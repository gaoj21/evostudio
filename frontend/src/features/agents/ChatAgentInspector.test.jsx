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
