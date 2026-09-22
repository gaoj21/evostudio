import { render, screen } from '@testing-library/react';
import React from 'react';
import { describe, expect, it } from 'vitest';

import TokenUsage from './TokenUsage.jsx';
import { compactTokens, tokenSuffix, tokenSummary } from './tokenUsageText.js';

const usage = { input_tokens: 1200, output_tokens: 34, total_tokens: 1234, reported_calls: 2 };

describe('token usage while work is in progress', () => {
  it('shows what has been reported so far, marked as live', () => {
    render(<TokenUsage usage={usage} running />);
    const line = screen.getByTestId('token-usage');
    expect(line).toHaveTextContent('Reported tokens so far: 1,234');
    expect(line).toHaveTextContent('input 1,200 / output 34');
    expect(line).toHaveAttribute('data-live', 'true');
  });

  it('says nothing has been reported yet rather than showing zero', () => {
    render(<TokenUsage usage={null} running />);
    const line = screen.getByTestId('token-usage');
    expect(line).toHaveTextContent('no provider usage reported yet');
    expect(line).not.toHaveTextContent('0');
  });

  it('reads as final once settled, and unavailable when nothing was reported', () => {
    const { rerender } = render(<TokenUsage usage={usage} />);
    expect(screen.getByTestId('token-usage')).toHaveTextContent('Reported tokens: 1,234');
    expect(screen.getByTestId('token-usage')).not.toHaveAttribute('data-live');
    rerender(<TokenUsage usage={{}} />);
    expect(screen.getByTestId('token-usage')).toHaveTextContent('Token usage unavailable');
  });

  it('updates in place as a poll brings larger totals', () => {
    const { rerender } = render(<TokenUsage usage={usage} running />);
    rerender(<TokenUsage usage={{ ...usage, total_tokens: 2000, input_tokens: 1900, output_tokens: 100, reported_calls: 3 }} running />);
    expect(screen.getByTestId('token-usage')).toHaveTextContent('Reported tokens so far: 2,000');
  });
});

describe('badge forms', () => {
  it('are empty when nothing was reported', () => {
    expect(tokenSummary(null)).toBe('');
    expect(tokenSuffix({ reported_calls: 0 }, true)).toBe('');
    expect(compactTokens(undefined)).toBe('');
  });

  it('say "so far" while running and shorten for node badges', () => {
    expect(tokenSuffix(usage, true)).toBe(' · 1,234 tokens so far');
    expect(tokenSummary(usage)).toBe('1,234 tokens');
    expect(compactTokens(usage)).toBe('1.2k tok');
    expect(compactTokens({ ...usage, total_tokens: 850 })).toBe('850 tok');
  });
});
