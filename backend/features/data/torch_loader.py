"""The common PyTorch Dataset/DataLoader adapter used by Studio inputs.

Keep records as dictionaries: torch's default collator would transpose fields,
convert numbers to tensors, and reject optional (None) values.
"""
import contextlib
import json
import inspect
import sys
from itertools import islice
from torch.utils.data import Dataset, IterableDataset, DataLoader


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


def execute_python(payload, emit=None, on_info=None, on_stage=None):
    """Called in a killable worker, never exec user code in the API process.

    Anything the Dataset code raises comes back as a UserCodeError naming
    the line in the user's code and carrying what it printed before it."""
    from backend.features.user_code import TailBuffer, UserCodeError, explain
    output = TailBuffer()
    try:
        return _execute_python(payload, emit, output, on_info, on_stage)
    except (Exception, SystemExit) as exc:
        # SystemExit too: a Dataset (or a library it calls) that exits would
        # otherwise end the worker without saying why.
        raise UserCodeError(explain(exc, payload['code'], output.getvalue(), FILENAME, 'Dataset code')) from exc


def selected(dataset, offset=0, limit=0, batch_size=1):
    """The Dataset's records offset..offset+limit (limit 0 = to the end).

    A map-style Dataset is asked only for the selected indices, so records
    before the offset are never read or preprocessed: skipping 500 records
    that each run a model must not run the model 500 times (a single run of
    record 500, a resumed batch). An IterableDataset can only be skipped by
    reading, one record at a time so nothing past the limit is read.
    """
    if not isinstance(dataset, Dataset):
        raise ValueError('build_dataset(resource, config) must return a torch.utils.data.Dataset or IterableDataset.')
    if isinstance(dataset, IterableDataset):
        rows = (row for chunk in batches(dataset, 1 if offset or limit else batch_size) for row in chunk)
        yield from islice(rows, offset, offset + limit if limit else None)
        return
    try:
        length = len(dataset)
    except (TypeError, NotImplementedError):
        length = None
    if length is None and not limit:
        raise ValueError('This map-style Dataset has no __len__, so Studio cannot tell where it ends. '
                         'Define __len__, set a Sample count, or make it an IterableDataset.')
    stop = offset + limit if limit else length
    if length is not None:
        stop = min(stop, length)
    loader = DataLoader(dataset, batch_size=batch_size, sampler=range(offset, max(offset, stop)),
                        num_workers=0, collate_fn=collate_records)
    for chunk in loader:
        yield from chunk


def _json_value(value, index, path=''):
    """value as JSON data. NumPy scalars become the Python number they hold;
    anything else JSON cannot carry is refused with where it is and what to
    do, rather than stringified."""
    import math
    where = f"Dataset item {index} field '{path}'" if path else f'Dataset item {index}'
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f'{where} has a {type(key).__name__} key {key!r}; use string keys.')
            out[key] = _json_value(item, index, f'{path}.{key}' if path else key)
        return out
    if isinstance(value, (list, tuple)):
        return [_json_value(item, index, f'{path}[{i}]') for i, item in enumerate(value)]
    numpy = sys.modules.get('numpy')
    if numpy is not None and isinstance(value, numpy.generic) and not isinstance(value, (numpy.datetime64, numpy.timedelta64, numpy.void)):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f'{where} is {value}; JSON has no NaN or infinity. Use None for a missing number.')
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    kind = type(value).__name__
    hint = ('convert it with .tolist() (or .item() for a single number)' if hasattr(value, 'tolist')
            else 'convert it with .isoformat()' if hasattr(value, 'isoformat')
            else 'convert it to a sorted list' if isinstance(value, (set, frozenset))
            else 'decode it to text (or base64) first' if isinstance(value, (bytes, bytearray))
            else 'convert it to str, int, float, bool, None, a list or a dict')
    raise ValueError(f'{where} is a {kind}, which is not JSON data; {hint}.')


def record(row, index):
    """One Dataset item as a JSON-ready record, or an error naming the item."""
    if not isinstance(row, dict):
        raise ValueError(f'Dataset item {index} is a {type(row).__name__}; each item must be a dict of field names to values.')
    if not row:
        raise ValueError(f'Dataset item {index} is an empty dict; each item needs at least one field. '
                         'Skip items with nothing to return in an IterableDataset, or filter them in build_dataset.')
    wrong = next((key for key in row if not isinstance(key, str) or not key or key == '_dataloader'), None)
    if wrong is not None:
        raise ValueError(f'Dataset item {index} has the field name {wrong!r}; use non-empty string names other than _dataloader.')
    try:
        json.dumps(row, allow_nan=False)
        return row
    except (TypeError, ValueError):
        return _json_value(row, index)


def _execute_python(payload, emit, output, on_info=None, on_stage=None):
    from backend.features.data.dataset_interface import arguments
    values = arguments(payload['code'], payload.get('config') or {})
    namespace = {'__name__': 'studio_dataset'}
    # Prints from a dataset should not corrupt the worker result protocol;
    # they are kept to explain a failure.
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        if on_stage: on_stage('Executing Dataset code and importing its dependencies')
        exec(compile(payload['code'], FILENAME, 'exec'), namespace)
        factory = namespace.get('build_dataset')
        if not callable(factory):
            raise ValueError('Define build_dataset(resource, config) returning a PyTorch Dataset.')
        signature = inspect.signature(factory)
        provided = {key:values[key] for key in signature.parameters if key in values}
        if 'resource' in signature.parameters: provided['resource'] = payload['resource']
        if 'config' in signature.parameters: provided['config'] = values
        signature.bind(**provided)
        if on_stage: on_stage('Initializing Dataset (build_dataset / __init__)')
        dataset = factory(**provided)
        if on_info is not None:
            try:
                length = len(dataset)
            except (TypeError, NotImplementedError):
                length = None
            remaining = None if length is None else max(0, length - payload.get('offset', 0))
            if remaining is not None and payload.get('record_limit', 0):
                remaining = min(remaining, payload['record_limit'])
            on_info({'dataset_length': length, 'selected_records': remaining})
        offset, limit = payload.get('offset', 0), payload.get('record_limit', 0)
        if emit is not None:
            if on_stage: on_stage('Reading the first batch (__getitem__ / __iter__ and preprocessing)')
            buffer, count = [], 0
            for index, row in enumerate(selected(dataset, offset, limit, payload['batch_size']), offset):
                buffer.append(record(row, index))
                if len(buffer) == payload['batch_size']:
                    emit(buffer); count += len(buffer); buffer = []
                    if on_stage: on_stage(f'Reading records after {offset + count}')
            if buffer: emit(buffer); count += len(buffer)
            return {'records':count}
        if payload.get('sample_limit') is not None:
            if on_stage: on_stage('Reading the sample record (__getitem__ / __iter__ and preprocessing)')
            if not isinstance(dataset, Dataset):
                raise ValueError('build_dataset must return a PyTorch Dataset or IterableDataset.')
            # The default sequential sampler calls len(dataset), which may scan
            # all files. Sampling needs only bounded indices, not dataset size.
            count = payload['sample_limit']
            loader = (islice(batches(dataset, 1), offset, offset + count) if isinstance(dataset, IterableDataset) else
                      DataLoader(dataset, batch_size=1, sampler=range(offset, offset + count),
                                 num_workers=0, collate_fn=collate_records))
            records = [record(chunk[0], index) for index, chunk in enumerate(islice(loader, count), offset)]
        else:
            # Select before materialization, without reading a full batch past the limit.
            records = [record(row, index) for index, row in enumerate(selected(dataset, offset, limit, payload['batch_size']), offset)]
    # Reject tensors / objects rather than silently stringify their values.
    json.dumps(records, allow_nan=False)
    return records
