import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { describe, expect, it } from 'vitest';

import RunOutcome from './RunOutcome.jsx';

describe('a failed run reads as a sentence first', () => {
  const failed = {
    status: 'failed',
    error: "Node 'decide' needs 'context': nothing feeds it and it was not supplied.",
    node_error: { code: 'missing_input', node: 'decide', field: 'context' },
    debug_error: 'Traceback (most recent call last):\n  File "runner.py"…',
  };

  it('shows the node, the field and the sentence, not the traceback', () => {
    render(<RunOutcome run={failed} />);
    const summary = screen.getByTestId('run-error-summary').textContent;
    expect(summary).toContain('decide');
    expect(summary).toContain('context');
    expect(summary).toContain("needs 'context'");
    expect(screen.queryByText(/Traceback/)).not.toBeInTheDocument();
  });

  it('keeps the traceback one click away', async () => {
    render(<RunOutcome run={failed} />);
    await userEvent.setup().click(screen.getByRole('button', { name: /show technical details/i }));
    expect(screen.getByText(/Traceback/)).toBeInTheDocument();
  });

  it('offers no details button when there is nothing behind it', () => {
    render(<RunOutcome run={{ status: 'failed', error: 'refused' }} />);
    expect(screen.queryByRole('button', { name: /technical details/i })).not.toBeInTheDocument();
  });
});

describe('a run you left or lost still has something to show', () => {
  it('says what happened to an abandoned run and what it went on to produce', () => {
    render(<RunOutcome run={{ status: 'abandoned', finished_after_abandon: 'success',
      late_result: { decision: 'alert' } }} />);
    expect(screen.getByText(/stopped waiting/)).toBeInTheDocument();
    expect(screen.getByText(/went on to/)).toBeInTheDocument();
    expect(screen.getByText('decision')).toBeInTheDocument();
    expect(screen.getByText('"alert"')).toBeInTheDocument();
  });

  it('says a lost run is lost rather than showing nothing', () => {
    render(<RunOutcome run={{ status: 'lost' }} />);
    expect(screen.getByText(/no longer knows this run/)).toBeInTheDocument();
    expect(screen.getByText(/No output was recorded/)).toBeInTheDocument();
  });
});


describe('a run that worked but could not remember says so', () => {
  const run = {
    status: 'success',
    result: { decision: 'suppress' },
    memory_error: 'Traceback (most recent call last):\n  File "table_store.py"\nsqlite3.OperationalError: unable to open database file',
  };

  it('warns with the last line of the failure, and keeps the result', () => {
    render(<RunOutcome run={run} />);
    const warning = screen.getByTestId('run-memory-warning').textContent;
    expect(warning).toMatch(/memory read or write failed/);
    expect(warning).toContain('unable to open database file');
    expect(warning).not.toContain('Traceback');
    expect(screen.getByText(/suppress/)).toBeInTheDocument();
  });

  it('says nothing about memory when it was written', () => {
    render(<RunOutcome run={{ status: 'success', result: {}, memory_written: [{ node: 'decide' }] }} />);
    expect(screen.queryByTestId('run-memory-warning')).not.toBeInTheDocument();
  });
});
