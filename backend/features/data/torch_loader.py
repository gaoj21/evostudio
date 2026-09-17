"""The common PyTorch Dataset/DataLoader adapter used by Studio inputs.

Keep records as dictionaries: torch's default collator would transpose fields,
convert numbers to tensors, and reject optional (None) values.
"""
import contextlib
import io
import json
import inspect
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


def execute_python(payload):
    """Called in a killable worker, never exec user code in the API process."""
    from backend.features.data.dataset_interface import arguments
    values = arguments(payload['code'], payload.get('config') or {})
    namespace = {'__name__': 'studio_dataset'}
    # Prints from a dataset should not corrupt the worker result protocol.
    with contextlib.redirect_stdout(io.StringIO()):
        exec(compile(payload['code'], '<dataloader.py>', 'exec'), namespace)
        factory = namespace.get('build_dataset')
        if not callable(factory):
            raise ValueError('Define build_dataset(resource, config) returning a PyTorch Dataset.')
        signature = inspect.signature(factory)
        provided = {key:values[key] for key in signature.parameters if key in values}
        if 'resource' in signature.parameters: provided['resource'] = payload['resource']
        if 'config' in signature.parameters: provided['config'] = values
        signature.bind(**provided)
        dataset = factory(**provided)
        records = [row for chunk in batches(dataset, payload['batch_size']) for row in chunk]
    # Reject tensors / objects rather than silently stringify their values.
    json.dumps(records, allow_nan=False)
    return records
