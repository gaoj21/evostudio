import { useEffect, useState } from 'react';

// Three-pane side by side needs real width. Below that the panes stack and the
// user picks one; a foldable crosses these live, so the layout is derived from
// matchMedia rather than read once at start-up.
export const PHONE_MAX = 699;
export const TABLET_MAX = 1023;

export function useLayoutMode() {
  // Returns null when the viewport width is not knowable yet — a hidden or
  // not-yet-composited tab reports 0, and treating that as "the narrowest
  // possible screen" collapses a desktop window to the phone layout.
  const read = () => {
    if (typeof window === 'undefined') return null;
    const w = window.innerWidth || document.documentElement?.clientWidth || 0;
    if (!w) return null;
    if (w <= PHONE_MAX) return 'phone';
    if (w <= TABLET_MAX) return 'tablet';
    return 'desktop';
  };
  const [mode, setMode] = useState(() => read() || 'desktop');
  useEffect(() => {
    const onResize = () => {
      const next = read();
      // No reading means no information: keep whatever the layout already is
      // rather than reflowing on a measurement that is not real.
      if (next) setMode(next);
    };
    window.addEventListener('resize', onResize);
    // Orientation changes on some devices do not fire resize, and browsers may
    // throttle it; a media-query listener fires reliably the moment a
    // breakpoint is crossed, which is exactly the fold/unfold transition.
    window.addEventListener('orientationchange', onResize);
    const queries = [
      window.matchMedia(`(max-width: ${PHONE_MAX}px)`),
      window.matchMedia(`(max-width: ${TABLET_MAX}px)`),
    ];
    queries.forEach((q) => q.addEventListener('change', onResize));
    onResize();
    return () => {
      window.removeEventListener('resize', onResize);
      window.removeEventListener('orientationchange', onResize);
      queries.forEach((q) => q.removeEventListener('change', onResize));
    };
  }, []);
  return mode;
}
