import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import ResultChat from './ResultChat.jsx';
import { api } from '../../api.js';
vi.mock('../../api.js', () => ({ api: { chatResults: vi.fn(), stopAssistant: vi.fn(), assistantTurn: vi.fn(), runningAssistantTurns: vi.fn() } }));
beforeEach(() => { localStorage.clear(); vi.resetAllMocks(); });
it('sends scope and follow-up history, preserving the conversation', async () => {
 api.chatResults.mockResolvedValue({ reply: 'The average is 12', count: 25, activity: [] });
 const props = {graphId:'g', run:{run_id:'r',batch_id:'b'}};
 const view=render(<ResultChat {...props}/>);
 fireEvent.change(screen.getByRole('combobox'), {target:{value:'batch'}});
 fireEvent.change(screen.getByLabelText('Question about results'), {target:{value:'Average?'}});
 fireEvent.click(screen.getByText('Send'));
 expect(await screen.findByText('The average is 12')).toBeInTheDocument();
 expect(api.chatResults.mock.calls[0][1]).toMatchObject({scope:'batch',run_id:'r'});
 fireEvent.change(screen.getByLabelText('Question about results'), {target:{value:'And the total?'}});
 fireEvent.click(screen.getByText('Send'));
 await screen.findAllByText('The average is 12');
 expect(api.chatResults.mock.calls[1][1].history).toHaveLength(2);
 view.unmount();
 render(<ResultChat {...props}/>);
 fireEvent.change(screen.getByRole('combobox'), {target:{value:'batch'}});
 expect(screen.getAllByText('Average?').length).toBeGreaterThan(0);
});
it('keeps the conversation and draft when the selected record changes',async()=>{
 api.chatResults.mockRejectedValue(new Error('Unavailable'));
 const view=render(<ResultChat graphId="g" run={{run_id:'r'}}/>);
 fireEvent.change(screen.getByLabelText('Question about results'),{target:{value:'Count?'}});
 fireEvent.click(screen.getByText('Send'));
 expect(await screen.findByRole('alert')).toHaveTextContent('Unavailable');
 expect(screen.getByLabelText('Question about results')).toHaveValue('Count?');
 expect(screen.getByText('Retry')).toBeEnabled();
 view.rerender(<ResultChat graphId="g" run={{run_id:'other'}}/>);
 expect(screen.getByLabelText('Question about results')).toHaveValue('Count?');
 expect(screen.getByText('Record · r')).toBeInTheDocument();
 fireEvent.click(screen.getByText('Use selected record'));
 expect(screen.getByText('Record · other')).toBeInTheDocument();
 expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});
it('starts a new session and resumes an older result conversation with its own context',async()=>{
 api.chatResults.mockImplementation(async(g, body)=>({reply:`Answer: ${body.message}`,count:1}));
 const props={graphId:'g',run:{run_id:'r'}};
 const view=render(<ResultChat {...props}/>);
 const ask=async text=>{
  fireEvent.change(screen.getByLabelText('Question about results'),{target:{value:text}});
  fireEvent.click(screen.getByRole('button',{name:'Send'}));
  await screen.findByText(`Answer: ${text}`);
 };
 await ask('First calculation');
 fireEvent.click(screen.getByRole('button',{name:'New session'}));
 expect(screen.queryByText('Answer: First calculation')).not.toBeInTheDocument();
 await ask('Second calculation');
 expect(api.chatResults.mock.calls[1][1].history).toEqual([]);
 fireEvent.click(screen.getByRole('button',{name:'History (2)'}));
 fireEvent.click(screen.getByRole('button',{name:'Open session First calculation'}));
 await ask('Continue first');
 const history=api.chatResults.mock.calls[2][1].history;
 expect(history.some(m=>m.content==='First calculation')).toBe(true);
 expect(history.some(m=>m.content==='Second calculation')).toBe(false);
 view.unmount();
 render(<ResultChat {...props}/>);
 expect(screen.getByText('Answer: Continue first')).toBeInTheDocument();
 expect(screen.getByRole('button',{name:'History (2)'})).toBeInTheDocument();
});
it('keeps one history list across data scopes and uses a closeable history drawer within the chat',async()=>{
 api.chatResults.mockResolvedValue({reply:'Saved answer',count:1});
 render(<ResultChat graphId="g" run={{run_id:'r',batch_id:'b'}}/>);
 fireEvent.change(screen.getByLabelText('Question about results'),{target:{value:'Keep this conversation'}});
 fireEvent.click(screen.getByRole('button',{name:'Send'}));
 await screen.findByText('Saved answer');
 fireEvent.change(screen.getByRole('combobox'),{target:{value:'all'}});
 expect(screen.getByText('Saved answer')).toBeInTheDocument();
 fireEvent.click(screen.getByRole('button',{name:'History (1)'}));
 const drawer=screen.getByRole('dialog',{name:'Conversation history'});
 expect(drawer.closest('.result-chat')).not.toBeNull();
 expect(drawer).toHaveAttribute('aria-modal', 'false');
 expect(screen.getByLabelText('Search conversations')).toHaveFocus();
 fireEvent.click(screen.getByRole('button',{name:'Close history'}));
 expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
 expect(screen.getByRole('button',{name:'History (1)'})).toHaveFocus();
});
it('stops the backend request, ignores a late answer, and can send again',async()=>{
 let resolve;
 api.chatResults.mockImplementationOnce(()=>new Promise(done=>{resolve=done;})).mockResolvedValue({reply:'Next answer'});
 api.stopAssistant.mockResolvedValue({status:'stopping'});
 render(<ResultChat graphId="g" run={{run_id:'r'}}/>);
 fireEvent.change(screen.getByLabelText('Question about results'),{target:{value:'Slow computation'}});
 fireEvent.click(screen.getByRole('button',{name:'Send'}));
 fireEvent.click(await screen.findByRole('button',{name:'■ Stop'}));
 expect(await screen.findByText('Stopped.')).toBeInTheDocument();
 expect(api.stopAssistant).toHaveBeenCalledWith('g',api.chatResults.mock.calls[0][1].request_id);
 expect(api.chatResults.mock.calls[0][2].signal.aborted).toBe(true);
 resolve({reply:'Late result'});
 fireEvent.change(screen.getByLabelText('Question about results'),{target:{value:'Next question'}});
 fireEvent.click(screen.getByRole('button',{name:'Send'}));
 expect(await screen.findByText('Next answer')).toBeInTheDocument();
 expect(screen.queryByText('Late result')).not.toBeInTheDocument();
});

it('sends consecutive questions on HTTP origins without randomUUID', async () => {
 const crypto = globalThis.crypto;
 vi.stubGlobal('crypto', { getRandomValues: crypto.getRandomValues.bind(crypto) });
 try {
  api.chatResults.mockImplementation(async (_g, body) => ({reply:`Answer: ${body.message}`}));
  render(<ResultChat graphId="g" run={{run_id:'r'}}/>);
  for (const question of ['First question', 'Next question']) {
   fireEvent.change(screen.getByLabelText('Question about results'),{target:{value:question}});
   fireEvent.click(screen.getByRole('button',{name:'Send'}));
   expect(await screen.findByText(`Answer: ${question}`)).toBeInTheDocument();
  }
  const [first, second] = api.chatResults.mock.calls.map(call => call[1]);
  expect(first.request_id).toMatch(/^[a-f0-9]{32}$/);
  expect(second.request_id).not.toBe(first.request_id);
  expect(second.history).toHaveLength(2);
 } finally { vi.unstubAllGlobals(); }
});

it('keeps answering after the results page is left and shows the answer on return', async () => {
 api.runningAssistantTurns.mockResolvedValue({ turns: [] });
 api.chatResults.mockImplementation(async (_g, body) => ({ turn_id: body.request_id, status: 'running' }));
 api.assistantTurn.mockResolvedValue({ status: 'running', elapsed: 4 });
 const props = { graphId: 'g', run: { run_id: 'r' } };
 const view = render(<ResultChat {...props}/>);
 fireEvent.change(screen.getByLabelText('Question about results'), { target: { value: 'Accuracy?' } });
 fireEvent.click(screen.getByRole('button', { name: 'Send' }));
 expect(await screen.findByText('Analyzing results… (4s)')).toBeInTheDocument();
 expect(api.chatResults.mock.calls[0][1].background).toBe(true);
 const turnId = api.chatResults.mock.calls[0][1].request_id;
 view.unmount();
 api.assistantTurn.mockResolvedValue({ turn_id: turnId, status: 'done', result: { reply: 'Accuracy is 0.9', count: 10, activity: [] } });
 render(<ResultChat {...props}/>);
 expect(await screen.findByText('Accuracy is 0.9')).toBeInTheDocument();
 expect(screen.getByText('Accuracy?', { selector: 'article > div' })).toBeInTheDocument();
 expect(api.assistantTurn).toHaveBeenLastCalledWith('g', turnId);
 expect(screen.getByRole('button', { name: 'Send' })).toBeInTheDocument();
});

it('re-attaches to a running question and Stop cancels it on the server', async () => {
 localStorage.setItem('result-chat:g:page:pending-turn', JSON.stringify({ turnId: 'turn-9', sessionId: 'gone', question: 'Slow?' }));
 api.assistantTurn.mockResolvedValue({ turn_id: 'turn-9', status: 'running', elapsed: 30 });
 api.stopAssistant.mockResolvedValue({ status: 'stopping' });
 render(<ResultChat graphId="g" run={{ run_id: 'r' }}/>);
 expect(await screen.findByText('Analyzing results… (30s)')).toBeInTheDocument();
 fireEvent.click(screen.getByRole('button', { name: '■ Stop' }));
 expect(await screen.findByText('Stopped.')).toBeInTheDocument();
 expect(api.stopAssistant).toHaveBeenCalledWith('g', 'turn-9');
 expect(localStorage.getItem('result-chat:g:page:pending-turn')).toBeNull();
});

it('offers a copy button on questions and answers', async () => {
 const writeText = vi.fn().mockResolvedValue();
 Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
 api.chatResults.mockResolvedValue({ reply: 'Twelve', activity: [] });
 render(<ResultChat graphId="g" run={{ run_id: 'r' }}/>);
 fireEvent.change(screen.getByLabelText('Question about results'), { target: { value: 'How many?' } });
 fireEvent.click(screen.getByRole('button', { name: 'Send' }));
 await screen.findByText('Twelve');
 fireEvent.click(screen.getByRole('button', { name: 'Copy your question' }));
 expect(writeText).toHaveBeenCalledWith('How many?');
 fireEvent.click(screen.getByRole('button', { name: 'Copy answer' }));
 expect(writeText).toHaveBeenLastCalledWith('Twelve');
});
