import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import ConnectionEditor from './ConnectionEditor.jsx';
import { edgeToFlow, flowToGraph } from './convert.js';
const nodes = [
  { id: 'a', position: {}, data: { outputs: [{ name: 'result', type: 'str' }] } },
  { id: 'b', position: {}, data: { inputs: [{ name: 'evidence', type: 'str' }] } },
];
function setup(extra = {}) {
  const props = { edge: edgeToFlow({ source: 'a', target: 'b' }), nodes, edges: [], onSave: vi.fn(), onClose: vi.fn(), ...extra };
  render(<ConnectionEditor {...props} />);
  return props;
}
describe('connection mapping editor', () => {
  it('maps differently named fields and saves an executable data edge', () => {
    const props = setup();
    expect(screen.getByText('Save connection')).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Source output for evidence'), { target: { value: 'result' } });
    fireEvent.click(screen.getByText('Save connection'));
    const edge = props.onSave.mock.calls[0][0];
    expect(edge.data).toMatchObject({ control_only: false, mappings: [{ from: 'result', to: 'evidence' }] });
    expect(flowToGraph({}, nodes, [edge]).edges).toEqual([{ source: 'a', target: 'b', mappings: [{ from: 'result', to: 'evidence' }] }]);
  });
  it('repairs an existing order-only connection without deleting it', () => {
    const props = setup({ edge: edgeToFlow({ source: 'a', target: 'b', control_only: true }) });
    fireEvent.change(screen.getByLabelText('Connection type'), { target: { value: 'data' } });
    fireEvent.change(screen.getByLabelText('Source output for evidence'), { target: { value: 'result' } });
    fireEvent.click(screen.getByText('Save connection'));
    expect(props.onSave.mock.calls[0][0].data.control_only).toBe(false);
  });
  it('creates order-only only by explicit choice', () => {
    const props = setup();
    fireEvent.change(screen.getByLabelText('Connection type'), { target: { value: 'order' } });
    fireEvent.click(screen.getByText('Save connection'));
    expect(props.onSave.mock.calls[0][0].data).toMatchObject({ control_only: true, mappings: [] });
  });
  it('blocks a second producer for an input', () => {
    setup({ edges: [edgeToFlow({ source: 'other', target: 'b', mappings: [{ from: 'value', to: 'evidence' }] })] });
    expect(screen.getByLabelText('Source output for evidence')).toBeDisabled();
    expect(screen.getByText('Save connection')).toBeDisabled();
  });
});
