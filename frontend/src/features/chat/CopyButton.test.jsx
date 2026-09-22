import React from 'react';
import { readFileSync } from 'node:fs';
import { render, screen, fireEvent } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import CopyButton from './CopyButton.jsx';

const css = ['../../styles.css', '../../platform.css']
  .map(path => readFileSync(new URL(path, import.meta.url), 'utf8')).join('\n');

it('never makes message text unselectable', () => {
  // Every rule that disables selection must stay away from chat messages.
  const rules = css.match(/[^{}]+\{[^}]*user-select:\s*none[^}]*\}/g) || [];
  for (const rule of rules) expect(rule).not.toMatch(/chat|agent-message|result-chat/);
  // Selected text in a message is visible even on the user's own bubble,
  // whose colours equal the global ::selection colours.
  expect(css).toMatch(/\.chat-bubble::selection[\s\S]*?background:\s*var\(--accent\)/);
  expect(css).toMatch(/\.chat-bubble,[\s\S]*?user-select:\s*text/);
});

it('falls back to a selection copy when the clipboard API is refused', async () => {
  Object.defineProperty(navigator, 'clipboard', { value: { writeText: vi.fn().mockRejectedValue(new Error('denied')) }, configurable: true });
  document.execCommand = vi.fn().mockReturnValue(true);
  render(<CopyButton text="hello" label="Copy reply" />);
  fireEvent.click(screen.getByRole('button', { name: 'Copy reply' }));
  expect(await screen.findByText('Copied')).toBeInTheDocument();
  expect(document.execCommand).toHaveBeenCalledWith('copy');
});
