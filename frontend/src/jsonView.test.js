import { describe, expect, it } from 'vitest';
import { unfold, pretty, isStructured, parseJsonLines } from './jsonView.js';

describe('JSON that arrives as a string is still JSON', () => {
  it('unfolds the string an LLM node hands back', () => {
    const out = { decision: '{"action":"alert","score":70}' };
    expect(unfold(out)).toEqual({ decision: { action: 'alert', score: 70 } });
  });

  it('keeps unfolding: JSON inside JSON inside JSON', () => {
    const out = { context: '{"profile":"{\\"cases\\":[1,2]}"}' };
    expect(unfold(out)).toEqual({ context: { profile: { cases: [1, 2] } } });
  });

  it('leaves a string that only looks numeric or boolean alone', () => {
    expect(unfold({ a: '42', b: 'true', c: 'null' })).toEqual({ a: '42', b: 'true', c: 'null' });
  });

  it('leaves prose alone, including prose with braces', () => {
    const text = 'Sleep Number {under review} — not JSON';
    expect(unfold(text)).toBe(text);
    expect(unfold('{not: json}')).toBe('{not: json}');
  });

  it('walks arrays', () => {
    expect(unfold(['{"a":1}', 2])).toEqual([{ a: 1 }, 2]);
  });

  it('pretty-prints the unfolded value, not the escaped one', () => {
    expect(pretty({ decision: '{"action":"alert"}' })).toBe('{\n  "decision": {\n    "action": "alert"\n  }\n}');
    expect(pretty('plain')).toBe('plain');
  });

  it('knows whether a tree is worth showing', () => {
    expect(isStructured('{"a":1}')).toBe(true);
    expect(isStructured('hello')).toBe(false);
    expect(isStructured(null)).toBe(false);
  });
});

describe('JSON Lines, as the run artifacts are written', () => {
  it('one document per line, blanks skipped', () => {
    expect(parseJsonLines('{"a":1}\n\n{"a":2}\n')).toEqual([{ a: 1 }, { a: 2 }]);
  });

  it('keeps a half-written last line visible rather than failing', () => {
    expect(parseJsonLines('{"a":1}\n{"a":')).toEqual([{ a: 1 }, '{"a":']);
  });
});
