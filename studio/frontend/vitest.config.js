import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The app renders in a browser, so the tests do too: jsdom lets a component
// test exercise the same effects and event handlers that ship.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.js'],
    include: ['src/**/*.test.{js,jsx}'],
  },
});
