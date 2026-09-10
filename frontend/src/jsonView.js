/**
 * JSON that arrives as a string is still JSON.
 *
 * An LLM node's output is text, so `decide` hands back
 * `{"decision": "{\"action\":\"alert\",\"score\":70,…}"}` — a string of
 * escaped quotes that nobody can read in the drawer. `unfold` parses any
 * string that is itself a JSON object or array, recursively, so what a
 * person sees is the structure, not the transport.
 *
 * Only objects and arrays are unfolded. A string holding `"42"` or
 * `"true"` stays a string: turning it into a number would change its
 * meaning, and there is nothing to expand anyway.
 */
export function unfold(value) {
  if (typeof value === 'string') {
    const parsed = parseStructured(value);
    return parsed === undefined ? value : unfold(parsed);
  }
  if (Array.isArray(value)) return value.map(unfold);
  if (value && typeof value === 'object') {
    const out = {};
    for (const [k, v] of Object.entries(value)) out[k] = unfold(v);
    return out;
  }
  return value;
}

function parseStructured(text) {
  const t = text.trim();
  if (!(t.startsWith('{') && t.endsWith('}')) && !(t.startsWith('[') && t.endsWith(']'))) {
    return undefined;
  }
  try {
    const v = JSON.parse(t);
    return v && typeof v === 'object' ? v : undefined;
  } catch {
    return undefined;
  }
}

/** Was any string inside `value` unfolded? Decides whether a tree is worth showing. */
export function isStructured(value) {
  const v = unfold(value);
  return v != null && typeof v === 'object';
}

/** The text the Copy button and the Raw view use. */
export function pretty(value) {
  const v = unfold(value);
  if (typeof v === 'string') return v;
  return JSON.stringify(v, null, 2);
}

/**
 * JSON Lines: one document per line, as the run artifacts are written.
 * Blank lines are skipped; a line that is not JSON is kept as a string so
 * a half-written last line is visible rather than a crash.
 */
export function parseJsonLines(text) {
  return String(text)
    .split('\n')
    .filter((line) => line.trim())
    .map((line) => {
      try {
        return JSON.parse(line);
      } catch {
        return line;
      }
    });
}
