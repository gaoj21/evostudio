import globals from 'globals';
import react from 'eslint-plugin-react';
import reactHooks from 'eslint-plugin-react-hooks';

/**
 * The point of linting here is one specific class of bug: a symbol that does
 * not exist. Vite bundles an undefined global without complaint and the app
 * white-screens at run time — which is exactly how a refactor that moved
 * helpers into another module shipped a blank page past a green build.
 *
 * Style is left alone; nothing here fails a build over formatting.
 */
export default [
  {
    files: ['src/**/*.{js,jsx}'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: { ...globals.browser, ...globals.es2021 },
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
    plugins: { react, 'react-hooks': reactHooks },
    rules: {
      'no-undef': 'error',
      // Without this, anything used only in JSX reads as unused.
      'react/jsx-uses-vars': 'error',
      'react/jsx-uses-react': 'error',
      // ignoreRestSiblings covers `const { drop, ...rest } = obj` — the
      // ordinary way to omit a key, where the named binding is the point.
      'no-unused-vars': ['warn', {
        argsIgnorePattern: '^_',
        varsIgnorePattern: '^React$',
        ignoreRestSiblings: true,
      }],
      // A hook whose dependencies are wrong is the other bug that bit us: a
      // stale closure, or an effect re-running on every render.
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
    },
  },
  {
    files: ['src/**/*.test.{js,jsx}', 'src/test/**/*.js'],
    languageOptions: { globals: { ...globals.browser, ...globals.node } },
  },
];
