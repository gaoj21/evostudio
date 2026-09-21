"""Regressions for the data layer: JSON containers, dataset references,
DataLoader reference inputs, stream timeouts, cache keys and user-code errors."""
import json
import pytest
from fastapi import HTTPException
from backend.api import graphs
from backend.api.sources import SourceError
from backend.features.data import data_resources, dataloaders, dataset_stream, user_datasets
from backend.features.data.json_records import parse_json_records


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    for module in (data_resources, dataloaders, user_datasets):
        monkeypatch.setattr(module, 'data_path', lambda *parts: tmp_path.joinpath(*parts))
    monkeypatch.setattr(graphs, 'GRAPHS_DIR', tmp_path / 'graphs')
    (tmp_path / 'graphs').mkdir()
    return tmp_path


def _dataset(records):
    records, fields = records, list(dict.fromkeys(k for r in records for k in r))
    item = {'id': 'a' * 32, 'name': 'd', 'filename': 'd.json', 'created_at': 'x', 'size_bytes': 1,
            'row_count': len(records), 'fields': fields, 'records': records}
    user_datasets.save(item)
    return item['id']


def test_json_record_with_records_field_is_not_unwrapped():
    assert parse_json_records('{"id":1,"records":[{"a":1}]}\n') == [{'id': 1, 'records': [{'a': 1}]}]
    assert parse_json_records('{"id":1,"records":[1,2]}') == [{'id': 1, 'records': [1, 2]}]
    # A lone container document is still unwrapped; JSONL documents never are.
    assert parse_json_records('{"records":[{"a":1},{"a":2}]}') == [{'a': 1}, {'a': 2}]
    assert parse_json_records('{"records":[{"a":1}]}\n{"records":[{"a":2}]}\n') == [
        {'records': [{'a': 1}]}, {'records': [{'a': 2}]}]


def test_dataset_used_through_dataloader_wrapper_cannot_be_deleted(data_dir):
    dataset_id = _dataset([{'name': 'A'}])
    node = {'name': 'in', 'kind': 'source', 'source': {'type': 'dataloader', 'loader': 'source',
            'source_config': {'type': 'user_dataset', 'dataset_id': dataset_id}}}
    (graphs.GRAPHS_DIR / 'g1.json').write_text(json.dumps({'id': 'g1', 'name': 'W', 'tasks': [node], 'edges': []}))
    assert user_datasets.references(dataset_id) == [{'graph_id': 'g1', 'graph_name': 'W', 'node': 'in'}]
    with pytest.raises(HTTPException) as caught:
        user_datasets.delete_dataset(dataset_id)
    assert caught.value.status_code == 409
    assert user_datasets.path_for(dataset_id).is_file()


def test_field_mapping_keeps_shared_reference_inputs(data_dir):
    dataset_id = _dataset([{'name': 'A', 'extra': 1}, {'name': 'B', 'extra': 2}])
    config = {'type': 'dataloader', 'loader': 'source', 'source_config': {'type': 'user_dataset', 'dataset_id': dataset_id},
              'field_mapping': {'name': 'label'}, 'reference_inputs': {'shared': [{'x': 1}]}}
    rows = dataloaders.records(config)
    assert [{k: v for k, v in r.items() if k != '_dataloader'} for r in rows] == [
        {'label': 'A', 'shared': [{'x': 1}]}, {'label': 'B', 'shared': [{'x': 1}]}]
    with pytest.raises(SourceError, match='overlap shared reference'):
        dataloaders.records({**config, 'field_mapping': {'name': 'shared'}})


def test_attaching_resource_keeps_cache_and_snapshot(data_dir, monkeypatch):
    resource = data_resources.create_from_records([{'a': 1}, {'a': 2}], 'r')
    config = {'type': 'dataloader', 'loader': 'auto', 'resource_id': resource['id']}
    calls = []
    original = dataloaders._prepare
    monkeypatch.setattr(dataloaders, '_prepare', lambda c: calls.append(1) or original(c))
    first = dataloaders.records(config)
    manifest = data_resources.path_for(resource['id']) / 'manifest.json'
    item = json.loads(manifest.read_text())
    manifest.write_text(json.dumps({**item, 'workspace_graph_ids': ['g1'], 'name': 'renamed'}))
    second = dataloaders.records(config)
    assert len(calls) == 1
    assert [r['_dataloader'] for r in first] == [r['_dataloader'] for r in second]


def test_stream_uses_configured_batch_timeout(data_dir, monkeypatch):
    resource = data_resources.create_from_records([{'a': 1}], 'r')
    code = ('import time\nfrom torch.utils.data import IterableDataset\n'
            'class Slow(IterableDataset):\n    def __iter__(self):\n        time.sleep(60)\n        yield {"a": 1}\n'
            'def build_dataset(resource): return Slow()\n')
    config = {'type': 'dataloader', 'loader': 'python', 'resource_id': resource['id'], 'code': code, 'batch_timeout': 3600}
    ticks = [0]
    real_sleep = dataset_stream.time.sleep

    class Clock:
        @staticmethod
        def monotonic():
            ticks[0] += 1
            return ticks[0]

        @staticmethod
        def sleep(_):
            real_sleep(.001)
    monkeypatch.setattr(dataset_stream, 'time', Clock)
    with pytest.raises(SourceError, match='within 3600 seconds'):
        list(dataset_stream.chunks(config))
    assert ticks[0] > 3600


def test_torch_dataset_error_names_user_line_and_output():
    from backend.features.data.torch_loader import execute_python
    from backend.features.user_code import UserCodeError
    code = ('def build_dataset(resource):\n'
            '    print("loading rows")\n'
            '    return {}["missing"]\n')
    with pytest.raises(UserCodeError) as caught:
        execute_python({'code': code, 'resource': {}, 'config': {}, 'batch_size': 2})
    message = str(caught.value)
    assert message.startswith("Dataset code line 3: KeyError: 'missing'")
    assert 'return {}["missing"]' in message and 'loading rows' in message


def test_dataloader_user_error_is_a_422_with_line_and_log(data_dir):
    resource = data_resources.create_from_records([{'a': 1}], 'r')
    code = ('import sys\nfrom torch.utils.data import Dataset\n'
            'class Rows(Dataset):\n    def __len__(self): return 1\n'
            '    def __getitem__(self, i):\n        print("reading", i, file=sys.stderr)\n        return 1 / 0\n'
            'def build_dataset(resource): return Rows()\n')
    with pytest.raises(HTTPException) as caught:
        dataloaders.preview({'type': 'dataloader', 'loader': 'python', 'resource_id': resource['id'],
                             'code': code, 'preview_mode': 'sample'})
    assert caught.value.status_code == 422
    assert caught.value.detail.startswith('Dataset code line 7: ZeroDivisionError: division by zero')
    assert 'reading 0' in caught.value.detail


def test_evaluator_explain_api_unchanged():
    from backend.features.evaluation import python_evaluator
    with pytest.raises(python_evaluator.UserCodeError) as caught:
        python_evaluator.execute({'code': 'def evaluate(records):\n    print("hi")\n    raise ValueError("bad")\n', 'records': []})
    assert 'line 3, in evaluate' in str(caught.value) and 'hi' in str(caught.value)
