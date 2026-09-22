import { readFileSync } from 'node:fs';
import { expect, it } from 'vitest';

const css = readFileSync('src/styles.css', 'utf8');
const app = readFileSync('src/App.jsx', 'utf8');

function rule(selector) {
  const match = css.match(new RegExp(`\\${selector}\\s*\\{([^}]*)\\}`));
  return match ? match[1] : '';
}

it('stacks every status bar instead of letting them land on each other', () => {
  // A batch, a second batch still running elsewhere and a single run can all
  // have a bar at once; each one used to be positioned at the same spot.
  expect(rule('.run-badge-bar')).toMatch(/position:\s*absolute/);
  expect(rule('.run-badge-bar')).toMatch(/flex-direction:\s*column/);
  expect(rule('.run-badge-group')).not.toMatch(/position:\s*absolute/);
  // One wrapper in the canvas, holding the groups.
  expect(app).toMatch(/className="run-badge-bar"/);
  expect(app.match(/className="run-badge-bar"/g)).toHaveLength(1);
});

it('keeps a long status bar inside the canvas', () => {
  // Token counts made these bars longer; they wrap rather than run off.
  expect(rule('.run-badge-bar')).toMatch(/max-width/);
  expect(rule('.run-badge-group')).toMatch(/flex-wrap:\s*wrap/);
});
