import { act, renderHook } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { useStudioNavigation } from './useStudioNavigation.js';

describe('studio navigation', () => {
  it('opens the unified library by default', () => {
    const { result } = renderHook(() => useStudioNavigation('desktop'));
    expect(result.current.leftTab).toBe('library');
  });

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
    expect(result.current.compactPane).toBe('library');
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

  it('uses the visible chat destination for each layout', () => {
    const { result, rerender } = renderHook(
      ({ layout }) => useStudioNavigation(layout),
      { initialProps: { layout: 'desktop' } }
    );
    act(() => result.current.openChat());
    expect(result.current.rightTab).toBe('chat');

    rerender({ layout: 'phone' });
    act(() => result.current.showCompact('setup'));
    act(() => result.current.openChat());
    expect(result.current.compactPane).toBe('chat');
  });
});


it.each(['desktop', 'tablet', 'phone'])('explicit inspector navigation is visible from Chat on %s', layout => {
  const { result } = renderHook(() => useStudioNavigation(layout));
  act(() => result.current.openChat());
  act(() => result.current.toggleLibrary());
  act(() => result.current.openInspector());
  expect(result.current.rightTab).toBe('inspector');
  if (layout !== 'desktop') {
    expect(result.current.compactPane).toBe('setup');
    expect(result.current.libraryOpen).toBe(false);
  }
});
