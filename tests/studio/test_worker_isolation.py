"""A worker's Python environment belongs to the user's code, not to Studio.

Studio's checkout has top-level folders called api, data, memory, features,
llm, tests, examples: with those ahead of site-packages, a Dataset's
`import data` or `import api` loaded Studio's folder instead of the installed
package or the module uploaded with the data. The worker therefore puts the
user's own folder first, the repository last, and starts in the resource's
files directory.
"""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from backend.features.chat import chat_control, chat_worker
from backend.features.data import data_resources, dataset_stream

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(data_resources, 'data_path', lambda *parts: tmp_path.joinpath(*parts))
    return tmp_path


def resource_with(files, data_dir):
    """A data resource holding the given {relative path: text} files."""
    item = data_resources.create_from_records([{'i': 1}], 'files')
    root = Path(item['root'])
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return item


def config_for(resource, code, **extra):
    return {'type': 'dataloader', 'loader': 'python', 'resource_id': resource['id'],
            'code': code, 'read_batch_size': 10, 'cache': False, **extra}


def read(resource, code, **extra):
    return [row for chunk in dataset_stream.chunks(config_for(resource, code, **extra)) for row in chunk]


@pytest.mark.parametrize('name', ['data', 'memory', 'api'])
def test_a_user_module_named_like_a_studio_folder_wins(data_dir, name):
    resource = resource_with({f'{name}/__init__.py': 'VALUE = "the user\'s module"\n'}, data_dir)
    code = ('from torch.utils.data import IterableDataset\n'
            f'import {name}\n'
            'class Rows(IterableDataset):\n'
            f'    def __iter__(self): yield {{"value": {name}.VALUE, "where": {name}.__file__}}\n'
            'def build_dataset(resource): return Rows()\n')
    rows = read(resource, code)
    assert rows[0]['value'] == "the user's module"
    assert str(REPO) not in rows[0]['where'] or 'data-resources' in rows[0]['where']


def test_studio_paths_come_last_and_the_worker_folder_is_not_on_the_path(tmp_path):
    """Nothing in Studio's checkout may shadow an installed package."""
    before = list(sys.path)
    try:
        chat_worker.isolate_path([tmp_path])
        entries = [str(Path(entry or '.').resolve()) for entry in sys.path]
        studio = [str(REPO), str(REPO / 'backend')]
        assert entries[0] == str(tmp_path.resolve())
        assert entries[-2:] == studio
        assert str((REPO / 'backend/features/chat').resolve()) not in entries
        site = next(entry for entry in entries if 'site-packages' in entry)
        assert entries.index(site) < entries.index(studio[0])
        # Idempotent: a multiprocessing spawn child re-imports the module.
        chat_worker.isolate_path([tmp_path])
        assert [str(Path(entry or '.').resolve()) for entry in sys.path] == entries
    finally:
        sys.path[:] = before


def test_studio_can_still_import_its_own_packages_with_the_repository_last():
    script = textwrap.dedent('''
        import sys
        sys.argv = ["chat_worker"]
        sys.path.insert(0, %r)
        from backend.features.chat import chat_worker
        chat_worker.isolate_path()
        import llm, memory, evoagentx, backend.features.data.torch_loader
        print("ok")
    ''') % str(REPO)
    done = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True,
                          cwd=str(Path(os.sep)), env=chat_control.worker_env())
    assert done.returncode == 0, done.stderr
    assert 'ok' in done.stdout


def test_user_code_runs_in_the_resource_folder_so_relative_paths_work(data_dir):
    resource = resource_with({'rows.jsonl': '{"i": 5}\n'}, data_dir)
    code = ('import json, os\n'
            'from torch.utils.data import IterableDataset\n'
            'class Rows(IterableDataset):\n'
            '    def __iter__(self):\n'
            '        with open("rows.jsonl") as handle:\n'
            '            for line in handle: yield {**json.loads(line), "cwd": os.getcwd()}\n'
            'def build_dataset(resource): return Rows()\n')
    rows = read(resource, code)
    assert rows[0]['i'] == 5
    assert Path(rows[0]['cwd']).resolve() == Path(resource['root']).resolve()


def test_a_pythonpath_in_the_server_environment_does_not_reach_workers(data_dir, monkeypatch, tmp_path):
    (tmp_path / 'sneaky_module.py').write_text('VALUE = 1\n')
    monkeypatch.setenv('PYTHONPATH', str(tmp_path))
    monkeypatch.setenv('DYLD_INSERT_LIBRARIES', '/nowhere/libfoo.dylib')
    monkeypatch.setenv('LD_PRELOAD', '/nowhere/libfoo.so')
    assert 'PYTHONPATH' not in chat_control.worker_env()
    assert 'DYLD_INSERT_LIBRARIES' not in chat_control.worker_env()
    assert 'LD_PRELOAD' not in chat_control.worker_env()
    resource = resource_with({}, data_dir)
    code = ('import sneaky_module\n'
            'def build_dataset(resource): return []\n')
    with pytest.raises(Exception, match='sneaky_module'):
        read(resource, code)


def test_openmp_settings_and_worker_defaults_survive_with_user_values_winning(monkeypatch):
    monkeypatch.setenv('OMP_NUM_THREADS', '8')
    monkeypatch.setenv('TOKENIZERS_PARALLELISM', 'true')
    env = chat_control.worker_env()
    assert env['OMP_NUM_THREADS'] == '8' and env['TOKENIZERS_PARALLELISM'] == 'true'
    monkeypatch.delenv('OMP_NUM_THREADS')
    monkeypatch.delenv('TOKENIZERS_PARALLELISM')
    env = chat_control.worker_env()
    assert env['OMP_NUM_THREADS'] == '1' and env['KMP_DUPLICATE_LIB_OK'] == 'TRUE'
    assert env['MKL_NUM_THREADS'] == '1' and env['OPENBLAS_NUM_THREADS'] == '1'
    assert env['TOKENIZERS_PARALLELISM'] == 'false'
    assert env['OBJC_DISABLE_INITIALIZE_FORK_SAFETY'] == 'YES'


def test_a_library_path_inside_another_conda_installation_is_dropped(monkeypatch):
    monkeypatch.setenv('LD_LIBRARY_PATH', os.pathsep.join(['/opt/miniconda3/lib', '/usr/local/lib']))
    assert chat_control.worker_env()['LD_LIBRARY_PATH'] == '/usr/local/lib'


def test_a_module_resolved_inside_studio_is_reported_as_a_conflict():
    from backend.features.user_code import hint

    class Fake:
        __path__ = [str(REPO / 'data')]
        __file__ = None
    import sys
    sys.modules['studio_shadow_probe'] = Fake()
    try:
        message = hint(ImportError('cannot import name "x"', name='studio_shadow_probe'))
    finally:
        del sys.modules['studio_shadow_probe']
    assert 'studio_shadow_probe' in message and str(REPO / 'data') in message
    assert sys.prefix in message


def test_a_native_library_that_cannot_load_says_to_reinstall_it():
    from backend.features.user_code import hint
    failure = ImportError("dlopen(/other/env/pkg/_c.so, 2): Library not loaded: @rpath/libomp.dylib",
                          name='somepkg', path='/other/env/pkg/_c.so')
    message = hint(failure)
    assert 'somepkg' in message and '/other/env/pkg/_c.so' in message
    assert '--force-reinstall' in message and sys.prefix in message


def test_studio_worker_kinds_still_run_with_the_new_path_order(tmp_path):
    value = chat_control.worker('evaluate_python', {
        'code': 'def evaluate(records):\n    return {"metrics": {"score": len(records)}}\n',
        'records': [{'a': 1}], 'config': {}}, timeout=120)
    assert value['report']['metrics']['score'] == 1
    batched = chat_control.worker('dataset_batches', {'records': [{'a': 1}, {'a': 2}, {'a': 3}], 'batch_size': 2}, timeout=120)
    assert [len(chunk) for chunk in batched] == [2, 1]


def test_openmp_heavy_packages_and_spacy_multiprocessing_run_in_one_worker(data_dir):
    pytest.importorskip('spacy')
    resource = resource_with({}, data_dir)
    code = ('import faiss, numpy, sklearn.linear_model, spacy, torch\n'
            'from torch.utils.data import IterableDataset\n'
            'class Rows(IterableDataset):\n'
            '    def __init__(self):\n'
            '        self.nlp = spacy.load("en_core_web_sm")\n'
            '    def __iter__(self):\n'
            '        texts = ["Ada Lovelace wrote in London." for _ in range(4)]\n'
            '        for doc in self.nlp.pipe(texts, n_process=2):\n'
            '            yield {"entities": [ent.text for ent in doc.ents]}\n'
            'def build_dataset(resource): return Rows()\n')
    rows = read(resource, code, preview_timeout=600, batch_timeout=600)
    assert len(rows) == 4 and all(row['entities'] for row in rows)


def test_a_worker_writes_nothing_of_the_protocol_to_stdout(data_dir):
    """A Dataset that prints must not corrupt how results come back."""
    resource = resource_with({}, data_dir)
    code = ('import sys\n'
            'from torch.utils.data import IterableDataset\n'
            'class Rows(IterableDataset):\n'
            '    def __iter__(self):\n'
            '        print(json.dumps({"fake": "result"}) if False else "chatty dataset")\n'
            '        print("to stderr too", file=sys.stderr)\n'
            '        yield {"i": 1}\n'
            'def build_dataset(resource): return Rows()\n')
    assert read(resource, code) == [{'i': 1}]
    assert json.dumps(read(resource, code))
