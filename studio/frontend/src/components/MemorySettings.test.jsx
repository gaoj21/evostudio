import { fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { describe, expect, it, vi } from 'vitest';

import MemorySettings, { isKept, memorySiblings, summarise, toggleField, toggleReadFrom } from './MemorySettings.jsx';

const OUTPUTS = ['verdict', 'why'];
const INPUTS = ['company', 'news'];

function node(data = {}) {
  return {
    id: 'judge',
    data: {
      use_long_term_memory: true,
      outputs: OUTPUTS.map((name) => ({ name })),
      inputs: INPUTS.map((name) => ({ name })),
      memory: null,
      ...data,
    },
  };
}

const SIBLINGS = [
  { name: 'judge', fields: [...INPUTS, ...OUTPUTS] },
  { name: 'investigate', fields: ['company', 'context'] },
];

// A field name can appear in more than one group now — `verdict` is both
// something to keep and something to read back — so queries say which.
function group(heading) {
  const head = [...document.querySelectorAll('.memory-group-head')]
    .find((h) => h.textContent === heading);
  if (!head) throw new Error(`no group headed ${heading}`);
  return within(head.parentElement);
}

const box = (heading, name) => group(heading).getByRole('checkbox', { name });

function setup(data, siblings = SIBLINGS) {
  const onUpdate = vi.fn();
  render(<MemorySettings node={node(data)} onUpdate={onUpdate} siblings={siblings} />);
  return { onUpdate, user: userEvent.setup() };
}

describe('isKept', () => {
  it('treats "all" as every field being kept', () => {
    expect(isKept(null, 'verdict')).toBe(true);
  });

  it('reads a real list literally', () => {
    expect(isKept(['verdict'], 'verdict')).toBe(true);
    expect(isKept(['verdict'], 'why')).toBe(false);
    expect(isKept([], 'verdict')).toBe(false);
  });
});

describe('toggleField', () => {
  it('turns "all" into a real list on the first untick', () => {
    // Otherwise the first click on a node that keeps everything would appear
    // to do nothing at all.
    expect(toggleField(null, 'why', OUTPUTS)).toEqual(['verdict']);
  });

  it('adds a field back', () => {
    const three = ['a', 'b', 'c'];
    expect(toggleField(['a'], 'c', three)).toEqual(['a', 'c']);
  });

  it('collapses back to "all" once everything is ticked again', () => {
    // A list naming every field would silently stop covering one added later,
    // so a full selection is stored as "all" rather than as that list.
    expect(toggleField(['verdict'], 'why', OUTPUTS)).toBeNull();
    expect(toggleField(null, 'why', OUTPUTS)).toEqual(['verdict']);
  });

  it('keeps the node\'s declared order, not the click order', () => {
    const three = ['a', 'b', 'c'];
    expect(toggleField([], 'c', three)).toEqual(['c']);
    expect(toggleField(['c'], 'a', three)).toEqual(['a', 'c']);
  });

  it('can end up keeping nothing', () => {
    expect(toggleField(['verdict'], 'verdict', OUTPUTS)).toEqual([]);
  });
});

describe('summarise', () => {
  it('says everything when nothing has been narrowed', () => {
    expect(summarise({ outputs: null, inputs: null }, OUTPUTS, INPUTS))
      .toBe('keeps all outputs + all inputs in a searchable corpus');
  });

  it('counts a partial selection', () => {
    expect(summarise({ outputs: ['verdict'], inputs: [] }, OUTPUTS, INPUTS))
      .toBe('keeps 1 of 2 outputs in a searchable corpus');
  });

  it('says which of the two stores it is', () => {
    // The distinction that matters: an exact record versus a similarity
    // search. They fail in different ways, so the summary has to name it.
    expect(summarise({ outputs: null, inputs: null, match: 'company' },
                     OUTPUTS, INPUTS))
      .toBe('keeps all outputs + all inputs in a table per company');
  });

  it('warns when the node would store nothing', () => {
    expect(summarise({ outputs: [], inputs: [] }, OUTPUTS, INPUTS))
      .toBe('nothing selected');
  });
});

describe('MemorySettings', () => {
  it('offers nothing to configure until memory is on', () => {
    setup({ use_long_term_memory: false });
    expect(screen.queryByText('OUTPUTS')).not.toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: /Long-term memory/ })).not.toBeChecked();
  });

  it('lists the node\'s own fields, all kept by default', () => {
    setup();
    OUTPUTS.forEach((name) => expect(box('Outputs', name)).toBeChecked());
    INPUTS.forEach((name) => expect(box('Inputs', name)).toBeChecked());
    expect(screen.getByText('keeps all outputs + all inputs in a searchable corpus'))
      .toBeInTheDocument();
  });

  it('drops a field from the policy when it is unticked', async () => {
    const { onUpdate, user } = setup();
    await user.click(box('Inputs', 'news'));
    await user.click(box('Outputs', 'why'));
    // Written whole: the reading side comes through untouched.
    expect(onUpdate).toHaveBeenCalledWith('judge', {
      memory: { outputs: null, inputs: ['company'], when: 'success', retrieve: 3,
                session_recall: 5, match: '', context: [], at: '',
                read_from: null, read: null },
    });
  });

  it('writes the whole policy, never a fragment of it', async () => {
    // A partial write would lose whichever settings were not being edited.
    const { onUpdate, user } = setup({
      memory: { outputs: ['verdict'], inputs: [], when: 'always', retrieve: 5 },
    });
    await user.click(box('Outputs', 'why'));

    // Ticking the last output back on means "all outputs" again — and the
    // settings not being edited come through untouched.
    const written = onUpdate.mock.calls[0][1].memory;
    expect(written).toEqual({ outputs: null, inputs: [], when: 'always', retrieve: 5,
                              session_recall: 5, match: '', context: [], at: '',
                              read_from: null, read: null });
  });

  it('warns when the selection would store nothing', () => {
    setup({ memory: { outputs: [], inputs: [] } });
    expect(screen.getByText(/would never write to its memory/)).toBeInTheDocument();
  });

  it('does not warn while something is still selected', () => {
    setup({ memory: { outputs: ['verdict'], inputs: [] } });
    expect(screen.queryByText(/would never write to its memory/)).not.toBeInTheDocument();
  });

  it('sets how many past runs come back', async () => {
    const { onUpdate } = setup();
    // Two numbers now — the session log and the long-term search are separate
    // decisions — so the query says which.
    fireEvent.change(screen.getByRole('spinbutton', { name: /Recall/ }),
                     { target: { value: '5' } });

    // A number, not the string the input hands over: the backend clamps an
    // integer and falls back to the default on anything else.
    expect(onUpdate.mock.calls.at(-1)[1].memory.retrieve).toBe(5);
  });

  it('says plainly that a recall of zero reads nothing back', () => {
    setup({ memory: { retrieve: 0 } });
    expect(screen.getByText(/similar past runs.*— none/)).toBeInTheDocument();
  });

  it('sets how much of the session it is reminded of', () => {
    const { onUpdate } = setup();
    fireEvent.change(screen.getByRole('spinbutton', { name: /Session/ }),
                     { target: { value: '2' } });
    expect(onUpdate.mock.calls.at(-1)[1].memory.session_recall).toBe(2);
  });

  it('can be told to ignore the session entirely', () => {
    setup({ memory: { session_recall: 0 } });
    expect(screen.getByText(/earlier steps of the same session.*— none/))
      .toBeInTheDocument();
  });

  it('opts into remembering failed runs', async () => {
    const { onUpdate, user } = setup();
    await user.click(screen.getByRole('checkbox', { name: /Remember failed runs/ }));
    expect(onUpdate.mock.calls.at(-1)[1].memory.when).toBe('always');
  });

  it('copes with a node that has no inputs', () => {
    setup({ inputs: [] }, [{ name: 'judge', fields: OUTPUTS }]);
    expect(screen.getByText('This node has no inputs.')).toBeInTheDocument();
    expect(box('Outputs', 'verdict')).toBeInTheDocument();
  });

  it('reads its own store by default', () => {
    setup();
    expect(box('Reads from', /judge\(its own\)/)).toBeChecked();
    expect(box('Reads from', 'investigate')).not.toBeChecked();
  });

  it('reads another node\'s memory when told to', async () => {
    const { onUpdate, user } = setup();
    await user.click(box('Reads from', 'investigate'));

    // A node that decides can draw on what the node that investigated learned.
    expect(onUpdate.mock.calls[0][1].memory.read_from).toEqual(['judge', 'investigate']);
  });

  it('offers the fields of whichever stores it reads', () => {
    setup({ memory: { read_from: ['investigate'] } });
    // `context` belongs to investigate, not to this node.
    expect(box('Reads back', 'context')).toBeInTheDocument();
  });

  it('narrows what a recalled entry brings back', async () => {
    const { onUpdate, user } = setup();
    await user.click(box('Reads back', 'why'));
    // Everything the store could give back, less the one field turned off.
    expect(onUpdate.mock.calls[0][1].memory.read).toEqual(['company', 'news', 'verdict']);
  });

  it('says when a node writes memory it will never read', () => {
    setup({ memory: { read_from: [] } });
    expect(screen.getByText(/writes memory without ever/)).toBeInTheDocument();
  });
});

describe('toggleReadFrom', () => {
  it('starts from its own store, not from every node', () => {
    // Reading everyone's history by default would be a surprising thing to do
    // with it, so narrowing begins at itself.
    expect(toggleReadFrom(null, 'investigate', 'judge')).toEqual(['judge', 'investigate']);
  });

  it('collapses back to "its own" when only that is left', () => {
    expect(toggleReadFrom(['judge', 'investigate'], 'investigate', 'judge')).toBeNull();
  });

  it('can end up reading nothing', () => {
    expect(toggleReadFrom(null, 'judge', 'judge')).toEqual([]);
  });
});

describe('fields from the rest of the workflow', () => {
  const WITH_SOURCE = [
    { name: 'judge', remembers: true, fields: [...INPUTS, ...OUTPUTS] },
    { name: 'feed', remembers: false, fields: ['company', 'as_of', 'window_end'] },
  ];

  it('offers a source node\'s fields to record', () => {
    // The observation window comes from the source node, not from anything
    // that reasons — leaving those out left nothing to date an entry by.
    setup({}, WITH_SOURCE);
    expect(box('Also record', 'as_of')).toBeInTheDocument();
    expect(box('Also record', 'window_end')).toBeInTheDocument();
  });

  it('does not offer to read a source node\'s memory', () => {
    // Only a node that keeps a memory has one to be read from.
    setup({}, WITH_SOURCE);
    expect(screen.queryByRole('checkbox', { name: 'feed' })).not.toBeInTheDocument();
    expect(box('Reads from', /judge/)).toBeInTheDocument();
  });

  it('records one when it is ticked', async () => {
    const { onUpdate, user } = setup({}, WITH_SOURCE);
    await user.click(box('Also record', 'as_of'));
    expect(onUpdate.mock.calls[0][1].memory.context).toEqual(['as_of']);
  });

  it('offers a recorded field as something to date entries by', () => {
    setup({ memory: { context: ['as_of'] } }, WITH_SOURCE);
    const dated = screen.getByRole('combobox', { name: /Dated by/ });
    expect([...dated.options].map((o) => o.value)).toContain('as_of');
  });

  it('warns that the write time orders nothing until one is picked', () => {
    setup({}, WITH_SOURCE);
    expect(screen.getByText(/written within the same minute/)).toBeInTheDocument();
  });
});


describe('memorySiblings', () => {
  const flow = [
    { id: 'feed', data: { kind: 'source', outputs: [{ name: 'company' }, { name: 'as_of' }] } },
    { id: 'judge', data: { use_long_term_memory: true, inputs: [{ name: 'company' }], outputs: [{ name: 'verdict' }] } },
    { id: 'lookup', data: { kind: 'tool', outputs: [{ name: 'hit' }] } },
  ];

  it('includes every node, source and tool alike', () => {
    // A node choosing which of the run's fields to record needs the source
    // node's outputs — the observation window comes from there, and leaving
    // them out left nothing to date an entry by.
    expect(memorySiblings(flow).map((s) => s.name)).toEqual(['feed', 'judge', 'lookup']);
  });

  it('marks which of them keep a memory to be read from', () => {
    const by = Object.fromEntries(memorySiblings(flow).map((s) => [s.name, s.remembers]));
    expect(by).toEqual({ feed: false, judge: true, lookup: false });
  });

  it('does not offer a task whose memory is switched off as a source', () => {
    const disabled = [{ id: 'plain', data: { kind: 'task', use_long_term_memory: false } }];
    expect(memorySiblings(disabled)[0].remembers).toBe(false);
  });

  it('collects each node\'s fields from both sides', () => {
    const judge = memorySiblings(flow).find((s) => s.name === 'judge');
    expect(judge.fields).toEqual(['company', 'verdict']);
  });

  it('copes with a node that declares nothing', () => {
    expect(memorySiblings([{ id: 'bare', data: {} }])[0].fields).toEqual([]);
    expect(memorySiblings(undefined)).toEqual([]);
  });
});

describe('choosing which store a node keeps', () => {
  // Coze keeps 数据库 and 知识库 apart and makes the developer pick. This
  // control is that choice, and it used to read as an addition ("also
  // recalls…") rather than a decision between two different things.
  it('offers a record per field, or a similarity search', () => {
    setup();
    const options = [...screen.getByLabelText('Keeps').querySelectorAll('option')]
      .map((o) => o.textContent);

    expect(options[0]).toBe('Past runs, searched by similarity');
    expect(options).toContain('A record per company');
  });

  it('explains what a table is, once one is chosen', () => {
    // The panel is controlled by the node it is given, so the choice is made
    // by rendering one that has it, not by driving the select.
    setup({ memory: { match: 'company', at: 'as_of' } });

    const said = document.body.textContent;
    expect(said).toContain('A table keyed by');
    expect(said).toContain('one row per');
    expect(said).toContain('either');
  });

  it('records the choice on the node', async () => {
    const { onUpdate, user } = setup();
    await user.selectOptions(screen.getByLabelText('Keeps'), 'company');

    expect(onUpdate).toHaveBeenCalledWith('judge',
      { memory: expect.objectContaining({ match: 'company' }) });
  });

  it('explains the corpus when no subject is tracked', () => {
    setup();
    expect(screen.getByText(/what resembles this/)).toBeInTheDocument();
  });
});
