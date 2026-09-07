import React, { useEffect, useState } from 'react';

const STORAGE_KEY = 'evoagentx-studio:theme';
const ORDER = ['system', 'light', 'dark'];
const LABEL = { system: 'System', light: 'Light', dark: 'Dark' };
const ICON = { system: '◐', light: '☀', dark: '☾' };

// `system` removes the attribute entirely so the stylesheet's
// prefers-color-scheme block takes over again.
function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === 'system') delete root.dataset.theme;
  else root.dataset.theme = theme;
}

export default function ThemeToggle() {
  const [theme, setTheme] = useState(() => {
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      return ORDER.includes(saved) ? saved : 'system';
    } catch {
      return 'system';
    }
  });

  useEffect(() => {
    applyTheme(theme);
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      /* storage unavailable; the theme still applies for this session */
    }
  }, [theme]);

  return (
    <button
      type="button"
      className="icon-btn"
      title={`Theme: ${LABEL[theme]} — click to change`}
      aria-label={`Theme: ${LABEL[theme]}`}
      onClick={() => setTheme((t) => ORDER[(ORDER.indexOf(t) + 1) % ORDER.length])}
    >
      {ICON[theme]}
    </button>
  );
}
