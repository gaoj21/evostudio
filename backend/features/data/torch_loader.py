"""The common PyTorch Dataset/DataLoader adapter used by Studio inputs.

Keep records as dictionaries: torch's default collator would transpose fields,
convert numbers to tensors, and reject optional (None) values.
"""
import contextlib
import json
import inspect
from itertools import islice
from torch.utils.data import Dataset, DataLoader


class RecordDataset(Dataset):
    def __init__(self, records):
        self.records = records

    def __len__(self):
        return len(self.records)

    def __getitem__(self, index):
        return self.records[index]


def collate_records(records):
    return records


def batches(dataset, batch_size):
    if not isinstance(dataset, Dataset):
        raise ValueError('build_dataset(resource, config) must return a torch.utils.data.Dataset or IterableDataset.')
    # Workflow execution has its own concurrency. Dataset workers must not
    # duplicate iterable inputs or re-order stateful workflow observations.
    return DataLoader(dataset, batch_size=batch_size, shuffle=False,
                      drop_last=False, num_workers=0, collate_fn=collate_records)


FILENAME = '<dataloader.py>'


def execute_python(payload, emit=None, on_info=None):
    """Called in a killable worker, never exec user code in the API process.

    Anything the Dataset code raises comes back as a UserCodeError naming
    the line in the user's code and carrying what it printed before it."""
    from backend.features.user_code import TailBuffer, UserCodeError, explain
    output = TailBuffer()
    try:
        return _execute_python(payload, emit, output, on_info)
    except Exception as exc:
        raise UserCodeError(explain(exc, payload['code'], output.getvalue(), FILENAME, 'Dataset code')) from exc


def _execute_python(payload, emit, output, on_info=None):
    from backend.features.data.dataset_interface import arguments
    values = arguments(payload['code'], payload.get('config') or {})
    namespace = {'__name__': 'studio_dataset'}
    # Prints from a dataset should not corrupt the worker result protocol;
    # they are kept to explain a failure.
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        exec(compile(payload['code'], FILENAME, 'exec'), namespace)
        factory = namespace.get('build_dataset')
        if not callable(factory):
            raise ValueError('Define build_dataset(resource, config) returning a PyTorch Dataset.')
        signature = inspect.signature(factory)
        provided = {key:values[key] for key in signature.parameters if key in values}
        if 'resource' in signature.parameters: provided['resource'] = payload['resource']
        if 'config' in signature.parameters: provided['config'] = values
        signature.bind(**provided)
        dataset = factory(**provided)
        if on_info is not None:
            try:
                length = len(dataset)
            except (TypeError, NotImplementedError):
                length = None
            selected = None if length is None else max(0, length - payload.get('offset', 0))
            if selected is not None and payload.get('record_limit', 0):
                selected = min(selected, payload['record_limit'])
            on_info({'dataset_length': length, 'selected_records': selected})
        if emit is not None:
            offset, limit = payload.get('offset', 0), payload.get('record_limit', 0)
            iterator = (row for chunk in batches(dataset, 1 if offset or limit else payload['batch_size']) for row in chunk)
            selected = islice(iterator, offset, offset + limit if limit else None)
            buffer, count = [], 0
            for row in selected:
                json.dumps(row, allow_nan=False)
                if not isinstance(row, dict): raise ValueError('Dataset items must be objects.')
                buffer.append(row)
                if len(buffer) == payload['batch_size']:
                    emit(buffer); count += len(buffer); buffer = []
            if buffer: emit(buffer); count += len(buffer)
            return {'records':count}
        if payload.get('sample_limit') is not None:
            # Batch size one prevents fetching a full configured batch for preview.
            records = [chunk[0] for chunk in islice(batches(dataset, 1), payload['sample_limit'])]
        else:
            offset, limit = payload.get('offset', 0), payload.get('record_limit', 0)
            # Select before materialization, without reading a full batch past the limit.
            if offset or limit:
                records = [chunk[0] for chunk in islice(batches(dataset, 1), offset, offset + limit if limit else None)]
            else:
                records = [row for chunk in batches(dataset, payload['batch_size']) for row in chunk]
    # Reject tensors / objects rather than silently stringify their values.
    json.dumps(records, allow_nan=False)
    return records
