import React, { useState } from 'react';
import { beforeEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import DatasetInput from './DatasetInput.jsx';
import { api } from '../../api.js';

vi.mock('../../api.js', () => ({ api: {
  listDatasets: vi.fn(), getDataset: vi.fn(), uploadDataset: vi.fn(),
  renameDataset: vi.fn(), deleteDataset: vi.fn(),
} }));
const item = { id: 'abc', name: 'Customers', row_count: 2, fields: ['name'], preview: [{name:'Alice'}, {name:'Bob'}] };
beforeEach(() => {
  vi.clearAllMocks();
  api.listDatasets.mockResolvedValue({datasets:[item]});
  api.getDataset.mockResolvedValue(item);
  api.uploadDataset.mockResolvedValue({...item, id:'new'});
  api.deleteDataset.mockResolvedValue({deleted:item.id});
});
function Harness({ onChange = () => {} }) {
  const [config, setConfig] = useState({ type:'user_dataset' });
  return <DatasetInput config={config} onChange={(value, outputs) => {setConfig(value); onChange(value, outputs);}} />;
}
it('selects a persisted dataset, previews it, and maps fields to canvas outputs', async () => {
  const change = vi.fn();
  render(<Harness onChange={change} />);
  await screen.findByText('Customers · 2 records');
  fireEvent.change(screen.getByLabelText('My datasets'), {target:{value:'abc'}});
  await screen.findByDisplayValue('Customers');
  expect(change).toHaveBeenCalledWith(expect.objectContaining({dataset_id:'abc'}), [expect.objectContaining({name:'name'})]);
  fireEvent.change(screen.getByLabelText('name → output'), {target:{value:'customer'}});
  expect(change).toHaveBeenLastCalledWith(expect.objectContaining({field_mapping:{name:'customer'}}), [expect.objectContaining({name:'customer'})]);
  fireEvent.click(screen.getByText('Delete dataset'));
  expect(api.deleteDataset).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText('Confirm delete'));
  await waitFor(() => expect(change).toHaveBeenLastCalledWith(expect.objectContaining({dataset_id:''}), []));
});
it('uploads and immediately selects the new dataset', async () => {
  const change = vi.fn();
  render(<Harness onChange={change} />);
  const file = new File(['name\nAlice'], 'my.csv', {type:'text/csv'});
  fireEvent.change(screen.getByLabelText('Upload dataset file'), {target:{files:[file]}});
  await waitFor(() => expect(api.uploadDataset).toHaveBeenCalledWith(file));
  await waitFor(() => expect(change).toHaveBeenCalledWith(expect.objectContaining({dataset_id:'new'}), expect.any(Array)));
});
it('shows upload errors without clearing the existing selection', async () => {
  api.uploadDataset.mockRejectedValue({body:{detail:'Invalid CSV header'}});
  const change = vi.fn(); render(<Harness onChange={change} />);
  fireEvent.change(screen.getByLabelText('Upload dataset file'), {target:{files:[new File(['x'],'bad.csv')]}});
  expect(await screen.findByRole('alert')).toHaveTextContent('Invalid CSV header');
  expect(change).not.toHaveBeenCalled();
});

it('switches an obligor dataset to a shared list output and preserves column mapping', async () => {
  const change = vi.fn(); render(<Harness onChange={change} />);
  await screen.findByText('Customers · 2 records');
  fireEvent.change(screen.getByLabelText('My datasets'), {target:{value:'abc'}});
  await screen.findByLabelText('Input role');
  fireEvent.change(screen.getByLabelText('Input role'), {target:{value:'reference'}});
  expect(change).toHaveBeenLastCalledWith(expect.objectContaining({input_mode:'reference', reference_field:'obligor_list'}),
    [expect.objectContaining({name:'obligor_list', type:'list'})]);
  fireEvent.change(screen.getByLabelText('Reference output name'), {target:{value:'customers'}});
  expect(change).toHaveBeenLastCalledWith(expect.objectContaining({reference_field:'customers'}),
    [expect.objectContaining({name:'customers', type:'list'})]);
  fireEvent.change(screen.getByLabelText('Input role'), {target:{value:'records'}});
  expect(change).toHaveBeenLastCalledWith(expect.objectContaining({input_mode:'records'}), [expect.objectContaining({name:'name'})]);
});
