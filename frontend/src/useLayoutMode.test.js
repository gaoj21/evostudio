import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { useLayoutMode } from './useLayoutMode.js';

/**
 * A foldable crosses these breakpoints while the app is open, so the layout is
 * derived live rather than read once. Pins the bug that shipped with it: a
 * hidden or not-yet-composited tab reports a width of 0, which read as "the
 * narrowest possible screen" and collapsed a desktop window to one pane.
 */

function setWidth(value) {
  Object.defineProperty(window, 'innerWidth', {
    configurable: true, writable: true, value,
  });
  Object.defineProperty(document.documentElement, 'clientWidth', {
    configurable: true, writable: true, value,
  });
}

beforeEach(() => {
  // jsdom has no matchMedia; the hook subscribes to it for breakpoint changes.
  window.matchMedia = vi.fn().mockImplementation((query) => ({
    matches: false,
    media: query,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }));
});

describe('useLayoutMode', () => {
  it.each([
    [380, 'phone'],
    [699, 'phone'],
    [700, 'tablet'],
    [1023, 'tablet'],
    [1024, 'desktop'],
    [1600, 'desktop'],
  ])('reports %ipx as %s', (width, expected) => {
    setWidth(width);
    const { result } = renderHook(() => useLayoutMode());
    expect(result.current).toBe(expected);
  });

  it('follows an unfold while the app is open', () => {
    setWidth(380);
    const { result } = renderHook(() => useLayoutMode());
    expect(result.current).toBe('phone');

    act(() => {
      setWidth(840);
      window.dispatchEvent(new Event('resize'));
    });
    expect(result.current).toBe('tablet');
  });

  it('keeps the layout when the width is unknowable', () => {
    setWidth(1280);
    const { result } = renderHook(() => useLayoutMode());
    expect(result.current).toBe('desktop');

    act(() => {
      setWidth(0);   // a hidden tab reports this
      window.dispatchEvent(new Event('resize'));
    });
    expect(result.current).toBe('desktop');
  });

  it('falls back to desktop when the first reading is already unknowable', () => {
    setWidth(0);
    const { result } = renderHook(() => useLayoutMode());
    expect(result.current).toBe('desktop');
  });

  it('stops listening when unmounted', () => {
    setWidth(1280);
    const remove = vi.spyOn(window, 'removeEventListener');
    const { unmount } = renderHook(() => useLayoutMode());
    unmount();
    expect(remove).toHaveBeenCalledWith('resize', expect.any(Function));
  });
});
