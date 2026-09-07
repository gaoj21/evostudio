import { act, renderHook } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { useStudioNavigation } from './useStudioNavigation.js';

describe('studio navigation', () => {
  it('opens Workspace in the persistent desktop sidebar', () => {
    const { result } = renderHook(() => useStudioNavigation('desktop'));
    act(() => result.current.openWorkspace());

    expect(result.current.leftTab).toBe('workspace');
    expect(result.current.libraryOpen).toBe(false);
  });

  it('opens Workspace in the tablet drawer', () => {
    const { result } = renderHook(() => useStudioNavigation('tablet'));
    act(() => result.current.openWorkspace());

    expect(result.current.leftTab).toBe('workspace');
    expect(result.current.libraryOpen).toBe(true);
  });

  it('opens Workspace as the visible phone pane', () => {
    const { result } = renderHook(() => useStudioNavigation('phone'));
    act(() => result.current.openWorkspace());

    expect(result.current.leftTab).toBe('workspace');
    expect(result.current.compactPane).toBe('nodes');
  });

  it('takes a selected phone node straight to its editor', () => {
    const { result } = renderHook(() => useStudioNavigation('phone'));
    act(() => result.current.revealSelection('detect'));
    expect(result.current.compactPane).toBe('setup');
  });

  it('does not move away from Chat when selection changes on a tablet', () => {
    const { result } = renderHook(() => useStudioNavigation('tablet'));
    act(() => result.current.revealSelection('detect'));
    expect(result.current.compactPane).toBe('chat');
  });
});
