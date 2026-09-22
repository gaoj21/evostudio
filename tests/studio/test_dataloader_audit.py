"""What a Dataset with real work in it runs into: selection that re-reads
records, values Python has and JSON does not, dependencies that are not
installed, time limits that cannot tell loading from reading, and a sample
that cannot be stopped. Records here are {i: int}: nothing domain-specific.
"""
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from backend.features.data import data_resources, dataloaders, dataset_stream
from backend.features.user_code import hint


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(data_resources, 'data_path', lambda *parts: tmp_path.joinpath(*parts))
    monkeypatch.setattr(dataloaders, 'data_path', lambda *parts: tmp_path.joinpath(*parts))
    dataloaders._verified.clear()
    return tmp_path


@pytest.fixture
def resource(data_dir):
    return data_resources.create_from_records([{'i': i} for i in range(50)], 'rows')


@pytest.fixture
def client():
    from backend.api.app import app
    return TestClient(app)


def counting_code(log):
    """A map-style Dataset that records every index it is asked for."""
    return (
        'from pathlib import Path\n'
        'from torch.utils.data import Dataset\n'
        'class Rows(Dataset):\n'
        '    def __len__(self): return 50\n'
        '    def __getitem__(self, index):\n'
        f'        with open({str(log)!r}, "a") as handle: handle.write(f"{{index}}\\n")\n'
        '        return {"i": index}\n'
        'def build_dataset(resource): return Rows()\n')


def config_for(resource, code, **extra):
    return {'type': 'dataloader', 'loader': 'python', 'resource_id': resource['id'], 'code': code,
            'read_batch_size': 1, 'cache': False, **extra}


def test_skipped_records_are_never_read_by_a_map_style_dataset(data_dir, resource):
    log = data_dir / 'asked.log'
    config = config_for(resource, counting_code(log), offset=40, n=2)
    assert [row for chunk in dataset_stream.chunks(config) for row in chunk] == [{'i': 40}, {'i': 41}]
    assert log.read_text().split() == ['40', '41']
    log.unlink()
    assert dataloaders.raw_records(config)[0] == [{'i': 40}, {'i': 41}]
    assert log.read_text().split() == ['40', '41']
    log.unlink()
    # The sample reads the first selected record, not record 0.
    assert dataloaders.raw_records(config, sample_limit=1)[0] == [{'i': 40}]
    assert log.read_text().split() == ['40']


def test_an_unsized_map_style_dataset_says_what_to_do(data_dir, resource):
    code = ('from torch.utils.data import Dataset\n'
            'class Rows(Dataset):\n'
            '    def __getitem__(self, index): return {"i": index}\n'
            'def build_dataset(resource): return Rows()\n')
    with pytest.raises(Exception, match='__len__'):
        list(dataset_stream.chunks(config_for(resource, code)))


@pytest.mark.parametrize('body, expected', [
    ('return {"i": i}\n', None),
    ('import numpy as np\n        return {"i": np.int64(i), "f": np.float32(0.5), "ok": np.bool_(True)}\n', None),
    ('import datetime\n        return {"when": datetime.date(2024, 1, 2)}\n', "field 'when' is a date"),
    ('return {"tags": {"a", "b"}}\n', "field 'tags' is a set"),
    ('import torch\n        return {"t": torch.tensor([1.0])}\n', "field 't' is a Tensor"),
    ('return {"x": float("nan")}\n', "field 'x' is nan"),
    ('return [i]\n', 'Dataset item 0 is a list'),
    ('return {}\n', 'Dataset item 0 is an empty dict'),
])
def test_values_json_cannot_carry_are_named_or_converted(data_dir, resource, body, expected):
    code = ('from torch.utils.data import Dataset\n'
            'class Rows(Dataset):\n'
            '    def __len__(self): return 2\n'
            '    def __getitem__(self, i):\n'
            f'        {body}'
            'def build_dataset(resource): return Rows()\n')
    config = config_for(resource, code, read_batch_size=2)
    if expected is None:
        rows = [row for chunk in dataset_stream.chunks(config) for row in chunk]
        assert rows[0]['i'] == 0
        if 'numpy' in body:
            # NumPy scalars are the Python numbers they hold, not an error.
            assert rows == [{'i': 0, 'f': 0.5, 'ok': True}, {'i': 1, 'f': 0.5, 'ok': True}]
        return
    with pytest.raises(Exception) as caught:
        list(dataset_stream.chunks(config))
    assert expected in str(caught.value)
    assert 'Dataset item' in str(caught.value)


def test_a_missing_package_or_spacy_model_says_how_to_install_it(data_dir, resource):
    code = ('import definitely_not_installed_xyz\n'
            'def build_dataset(resource): return []\n')
    with pytest.raises(Exception) as caught:
        list(dataset_stream.chunks(config_for(resource, code)))
    message = str(caught.value)
    assert 'definitely_not_installed_xyz' in message and '-m pip install definitely_not_installed_xyz' in message
    assert 'line 1' in message


def test_install_hints_name_the_package_and_studio_python():
    import sys
    assert 'scikit-learn' in hint(ModuleNotFoundError('No module named sklearn', name='sklearn'))
    assert sys.executable in hint(ModuleNotFoundError('No module named sklearn', name='sklearn'))
    model = hint(OSError("[E050] Can't find model 'en_core_web_sm'. It doesn't seem to be a Python package"))
    assert '-m spacy download en_core_web_sm' in model
    assert hint(ValueError('something else')) == ''


def test_slow_initialization_is_named_as_initialization(data_dir, resource, monkeypatch):
    slow_init = ('import time\n'
                 'from torch.utils.data import IterableDataset\n'
                 'class Rows(IterableDataset):\n'
                 '    def __iter__(self): yield {"i": 1}\n'
                 'def build_dataset(resource):\n'
                 '    time.sleep(120)\n'
                 '    return Rows()\n')
    ticks = [0]
    real_sleep = time.sleep

    class Clock:
        @staticmethod
        def monotonic():
            ticks[0] += 1
            return ticks[0]

        @staticmethod
        def sleep(_):
            real_sleep(.001)
    monkeypatch.setattr(dataset_stream, 'time', Clock)
    with pytest.raises(Exception, match='initialization') as caught:
        list(dataset_stream.chunks(config_for(resource, slow_init, preview_timeout=10, batch_timeout=10)))
    assert 'Sample time limit' in str(caught.value)


def test_a_slow_first_batch_is_not_reported_as_slow_initialization(data_dir, resource):
    slow_batch = ('import time\n'
                  'from torch.utils.data import IterableDataset\n'
                  'class Rows(IterableDataset):\n'
                  '    def __iter__(self):\n'
                  '        time.sleep(120)\n'
                  '        yield {"i": 1}\n'
                  'def build_dataset(resource): return Rows()\n')
    with pytest.raises(Exception) as caught:
        list(dataset_stream.chunks(config_for(resource, slow_batch, preview_timeout=3600, batch_timeout=10)))
    assert 'did not produce a batch within 10 seconds' in str(caught.value)
    assert 'Last stage' in str(caught.value)


def test_a_full_read_streams_batches_instead_of_one_timed_whole_read(data_dir, resource, monkeypatch):
    """A long dataset must not fail because the whole read took longer than a
    fixed multiple of one batch's time limit."""
    calls = []
    original = dataset_stream.read
    monkeypatch.setattr(dataset_stream, 'read', lambda *a, **kw: calls.append(kw or a) or original(*a, **kw))
    from backend.features.chat import chat_control
    monkeypatch.setattr(chat_control, 'worker', lambda *a, **kw: pytest.fail('A full read must not ship the dataset back in one result'))
    code = ('from torch.utils.data import IterableDataset\n'
            'class Rows(IterableDataset):\n'
            '    def __iter__(self):\n'
            '        for i in range(5): yield {"i": i}\n'
            'def build_dataset(resource): return Rows()\n')
    assert len(dataloaders.records(config_for(resource, code, read_batch_size=2))) == 5
    assert calls


def test_a_worker_that_dies_reports_its_last_output(data_dir, resource):
    code = ('import os\n'
            'from torch.utils.data import IterableDataset\n'
            'class Rows(IterableDataset):\n'
            '    def __iter__(self):\n'
            '        os.write(2, b"native library said goodbye")\n'
            '        os._exit(7)\n'
            '        yield {"i": 1}\n'
            'def build_dataset(resource): return Rows()\n')
    with pytest.raises(Exception) as caught:
        list(dataset_stream.chunks(config_for(resource, code)))
    assert 'exit code 7' in str(caught.value) and 'native library said goodbye' in str(caught.value)


def test_prepared_data_cache_stays_within_its_limit(data_dir, resource, monkeypatch):
    code = ('from torch.utils.data import IterableDataset\n'
            'class Rows(IterableDataset):\n'
            '    def __init__(self, tag): self.tag = tag\n'
            '    def __iter__(self): yield {"i": 1, "tag": self.tag}\n'
            'def build_dataset(resource, tag: str = "a"): return Rows(tag)\n')
    monkeypatch.setattr(dataloaders, 'CACHE_LIMIT_BYTES', 200)
    for tag in ('a', 'b', 'c'):
        dataloaders.records(config_for(resource, code, cache=True, reader_config={'tag': tag}))
    stored = list((data_dir / 'dataloader-cache').glob('*.json'))
    # Only the entry just written survives a limit smaller than two entries,
    # and it is the one the last read can still reuse.
    assert len(stored) == 1
    assert dataloaders.records(config_for(resource, code, cache=True, reader_config={'tag': 'c'}))[0]['tag'] == 'c'
    assert len(list((data_dir / 'dataloader-cache').glob('*.json'))) == 1


def test_resource_files_are_hashed_once_and_changes_are_still_caught(data_dir, resource, monkeypatch):
    config = {'type': 'dataloader', 'loader': 'auto', 'resource_id': resource['id']}
    hashed = []
    original = dataloaders.hashlib.sha256
    monkeypatch.setattr(dataloaders.hashlib, 'sha256', lambda *a: hashed.append(1) or original(*a))
    dataloaders.records(config)
    first = len(hashed)
    dataloaders.records(config)
    assert len(hashed) < first * 2
    path = data_resources.path_for(resource['id']) / 'files' / 'records.jsonl'
    path.write_text('{"i": 99}\n')
    with pytest.raises(Exception, match='Resource file changed'):
        dataloaders.records(config)


def test_the_interface_says_which_packages_are_missing(client):
    code = ('import spacy\n'
            'import definitely_not_installed_xyz\n'
            'nlp = spacy.load("xx_not_a_model")\n'
            'def build_dataset(resource): return []\n')
    response = client.post('/api/dataloaders/interface', json={'code': code})
    assert response.status_code == 200, response.text
    found = {item['name']: item for item in response.json()['dependencies']}
    assert found['definitely_not_installed_xyz']['available'] is False
    assert 'pip install definitely_not_installed_xyz' in found['definitely_not_installed_xyz']['install']
    assert found['xx_not_a_model']['kind'] == 'spacy model' and found['xx_not_a_model']['available'] is False
    assert 'spacy download xx_not_a_model' in found['xx_not_a_model']['install']


def test_a_slow_sample_can_be_stopped(client, data_dir, resource):
    code = ('import time\n'
            'from torch.utils.data import IterableDataset\n'
            'class Rows(IterableDataset):\n'
            '    def __iter__(self): yield {"i": 1}\n'
            'def build_dataset(resource):\n'
            '    time.sleep(300)\n'
            '    return Rows()\n')
    body = {'type': 'dataloader', 'loader': 'python', 'resource_id': resource['id'], 'code': code,
            'preview_mode': 'sample', 'preview_timeout': 3600, '_request_id': 'sample-test-1'}
    outcome = {}

    def sample():
        outcome['response'] = client.post('/api/dataloaders/preview', json=body)
    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    started = time.monotonic()
    while time.monotonic() - started < 30:
        stop = client.post('/api/dataloaders/preview/sample-test-1/stop')
        if stop.status_code == 200 and stop.json()['status'] in ('stopping', 'stopped'):
            break
        time.sleep(.2)
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert outcome['response'].status_code == 409
    assert 'stopped' in outcome['response'].json()['detail']


def test_reading_one_record_for_a_single_run_does_not_run_earlier_records(data_dir, resource):
    from backend.features.data import input_composition
    log = data_dir / 'asked.log'
    node = {'name': 'input', 'kind': 'source', 'outputs': [{'name': 'i'}],
            'source': config_for(resource, counting_code(log))}
    graph = {'id': 'g', 'tasks': [node], 'edges': [{'source': 'input', 'target': 'next'}]}
    assert input_composition.read_record(graph, node, 7) == {'i': 7}
    assert log.read_text().split() == ['7']


def test_group_ordering_and_provenance_work_on_any_field_names(data_dir, resource):
    code = ('from torch.utils.data import IterableDataset\n'
            'class Rows(IterableDataset):\n'
            '    def __iter__(self):\n'
            '        for row in [{"k": "b", "seq": 2}, {"k": "a", "seq": 2}, {"k": "a", "seq": 1}]: yield row\n'
            'def build_dataset(resource): return Rows()\n')
    rows = dataloaders.records(config_for(resource, code, group_by='k', order_by='seq', read_batch_size=10))
    assert [(row['k'], row['seq']) for row in rows] == [('a', 1), ('a', 2), ('b', 2)]
    assert [row['_dataloader']['group'] for row in rows] == ['a', 'a', 'b']
    assert json.dumps(rows)
