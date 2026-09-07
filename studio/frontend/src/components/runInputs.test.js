import { describe, expect, it } from 'vitest';

import { defaultFor, parseValue, toField } from './RunDialog.jsx';

/**
 * The framework validates run inputs against their declared JSON-schema type,
 * so the form has to send real types. Pins the bug this fixed: every input was
 * sent as a string, and a `list` or `dict` input was rejected outright — those
 * workflows could not be run from the UI at all.
 */

describe('parseValue', () => {
  it('passes strings through', () => {
    expect(parseValue('str', 'hello')).toEqual({ value: 'hello' });
  });

  it('turns a typed number into a number', () => {
    expect(parseValue('int', '42')).toEqual({ value: 42 });
    expect(parseValue('float', '1.5')).toEqual({ value: 1.5 });
  });

  it('rejects a non-integer for an int', () => {
    expect(parseValue('int', '1.5').error).toMatch(/whole number/);
    expect(parseValue('int', 'abc').error).toBeTruthy();
  });

  it('reads a checkbox as a real boolean', () => {
    expect(parseValue('bool', true)).toEqual({ value: true });
    expect(parseValue('bool', false)).toEqual({ value: false });
  });

  it('parses JSON for list and dict inputs', () => {
    expect(parseValue('list', '[1, 2]')).toEqual({ value: [1, 2] });
    expect(parseValue('dict', '{"a": 1}')).toEqual({ value: { a: 1 } });
  });

  it('says where the JSON is wrong instead of failing at run time', () => {
    expect(parseValue('list', '[not json').error).toMatch(/invalid JSON/);
  });

  it('refuses a JSON value of the wrong shape', () => {
    expect(parseValue('list', '{"a": 1}').error).toMatch(/array/);
    expect(parseValue('dict', '[1, 2]').error).toMatch(/object/);
  });

  it('treats an empty optional value as absent, not as empty text', () => {
    expect(parseValue('int', '')).toEqual({ value: undefined });
    expect(parseValue('list', '')).toEqual({ value: undefined });
  });
});

describe('defaultFor', () => {
  it('gives each type a starting value the field can render', () => {
    expect(defaultFor('bool')).toBe(false);
    expect(defaultFor('list')).toBe('[]');
    expect(defaultFor('dict')).toBe('{}');
    expect(defaultFor('str')).toBe('');
  });
});

describe('toField', () => {
  it('renders a prefilled structured value as editable JSON', () => {
    expect(toField('dict', { a: 1 })).toBe('{\n  "a": 1\n}');
    expect(toField('list', ['x'])).toBe('[\n  "x"\n]');
  });

  it('leaves text and booleans alone', () => {
    expect(toField('str', 'Gaucho')).toBe('Gaucho');
    expect(toField('bool', true)).toBe(true);
  });

  it('falls back to the type default when there is nothing to prefill', () => {
    expect(toField('list', undefined)).toBe('[]');
    expect(toField('str', null)).toBe('');
  });
});
