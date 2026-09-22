"""Isolated blocking calls so Stop can terminate sockets and model retries."""
import json
import os
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[2]

# Before any user code imports a native package: see chat_control.worker_env.
# Repeated here for workers started some other way.
for _key, _value in (('OMP_NUM_THREADS', '1'), ('MKL_NUM_THREADS', '1'),
                     ('OPENBLAS_NUM_THREADS', '1'), ('KMP_DUPLICATE_LIB_OK', 'TRUE'),
                     ('TOKENIZERS_PARALLELISM', 'false'), ('OBJC_DISABLE_INITIALIZE_FORK_SAFETY', 'YES')):
    os.environ.setdefault(_key, _value)

# Kinds that execute the user's own Python. They run with their own folder
# first on the import path and as their working directory.
USER_CODE_KINDS = ('dataset', 'dataset_stream', 'evaluate_python')


def isolate_path(first=()):
    """Order sys.path as: the user's folders, then the standard library and
    site-packages, then Studio's repository last.

    Python puts a script's own folder first, and Studio used to put its
    repository and backend/ folder before site-packages: a user's `import data`,
    `import memory` or `import api` then loaded one of Studio's folders instead
    of the installed package or the module uploaded with the data. Studio's own
    imports use the `backend.` prefix (and backend/'s top-level llm, memory and
    evoagentx), which still resolve from the end of the path. Idempotent: a
    multiprocessing spawn child re-imports this module with the parent's path.
    """
    here = Path(__file__).resolve().parent
    studio = [str(root.parent), str(root)]
    first = [str(Path(folder).resolve()) for folder in first]

    def keep(entry):
        try:
            resolved = str(Path(entry or '.').resolve())
        except OSError:
            return True
        return entry not in studio and resolved not in studio and resolved != str(here) and resolved not in first
    sys.path[:] = [*first, *[entry for entry in sys.path if keep(entry)], *studio]


isolate_path()


def _exit_with_parent(directory):
    """Stop when the process that started this worker is gone.

    Workers run in their own session so Stop can kill them as a group; that
    also means a server restart or crash would leave them running, and a
    streaming Dataset worker waits indefinitely for its next batch to be
    taken. Reparenting (the parent pid changes) or the vanished working
    directory both mean nobody will ever read the result.
    """
    import os
    import threading
    import time
    # The launcher passes its own pid: reading getppid() here would race a
    # parent that already exited before this process got going.
    parent = int(os.environ.get('STUDIO_WORKER_PARENT') or os.getppid())

    def gone():
        return os.getppid() != parent or not directory.exists()
    if gone():
        os._exit(3)

    def watch():
        while True:
            time.sleep(1)
            if gone():
                os._exit(3)
    threading.Thread(target=watch, daemon=True).start()


def main():
    kind, directory = sys.argv[1:]
    directory = Path(directory).resolve()
    _exit_with_parent(directory)
    payload = json.loads((directory / 'input.json').read_text())
    if kind in USER_CODE_KINDS:
        # Relative paths in user code resolve against the data resource's
        # files (resource["root"]), or a private scratch folder when there is
        # none, never against Studio's checkout.
        resource = payload.get('resource') if isinstance(payload.get('resource'), dict) else {}
        folder = Path(resource['root']).resolve() if resource.get('root') and Path(resource['root']).is_dir() else None
        if folder is not None:
            resource['root'] = str(folder)
        else:
            folder = directory / 'work'
            folder.mkdir(exist_ok=True)
        isolate_path([folder])
        os.chdir(folder)
    try:
        if kind == 'model':
            from llm import chat
            value = chat(None, payload['messages'])
        elif kind == 'collect':
            from backend.api import sources
            from backend.features.data.source_apis import collect_records
            config = payload['source']
            def emit(record):
                with (directory / 'records.jsonl').open('a') as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + '\n')
            def progress(done, total):
                (directory / 'stage.txt').write_text(json.dumps({'completed': done, 'total': total}))
            if config['type'] == 'dataloader':
                # A DataLoader over an API source: its reader collects the
                # range (collect_records), then its preprocessing applies.
                value = sources.records_from_source_node(payload)
                for record in value:
                    emit(record)
            else:
                value = collect_records(config, progress, on_record=emit)
        elif kind == 'preprocess':
            from backend.api.custom_tools import run_custom_tool
            value = run_custom_tool(payload['name'], payload['arguments'])
        elif kind == 'evaluate_python':
            from backend.features.evaluation.python_evaluator import execute_with_logs
            value = execute_with_logs(payload)
        elif kind == 'dataset_stream':
            import time
            def emit(rows):
                temporary = directory / 'chunk.tmp'
                temporary.write_text(json.dumps(rows, ensure_ascii=False, allow_nan=False))
                temporary.replace(directory / 'chunk.json')
                while (directory / 'chunk.json').exists(): time.sleep(.05)
            def on_info(info):
                temporary = directory / 'info.tmp'
                temporary.write_text(json.dumps(info))
                temporary.replace(directory / 'info.json')
            def stage(message):
                # What a timeout names as the last thing the Dataset did.
                temporary = directory / 'stage.tmp'
                temporary.write_text(message)
                temporary.replace(directory / 'stage.txt')
            stage('Importing PyTorch and Dataset runtime')
            from backend.features.data.torch_loader import execute_python
            value = execute_python(payload, emit=emit, on_info=on_info, on_stage=stage)
        elif kind == 'dataset_batches':
            from backend.features.data.torch_loader import RecordDataset, batches
            value = list(batches(RecordDataset(payload['records']), payload['batch_size']))
        elif kind == 'dataset':
            def stage(message):
                temporary = directory / 'stage.tmp'
                temporary.write_text(message)
                temporary.replace(directory / 'stage.txt')
            stage('Importing PyTorch and Dataset runtime')
            from backend.features.data.torch_loader import execute_python
            value = execute_python(payload, on_stage=stage)
        elif kind == 'verify_tool':
            from backend.features.library.tool_verification import verify
            value = verify(payload['spec'])
        elif kind == 'generate':
            from backend.api.chat_api import _workflow_from_goal
            value = _workflow_from_goal(payload['goal'], on_stage=lambda text: (directory / 'stage.txt').write_text(text))
        else:
            raise ValueError('Unknown assistant worker')
        result = {'value': value}
    except Exception as exc:
        # Errors raised by the user's own code already say where and why.
        result = {'error': str(exc) if getattr(exc, 'user_code', False) else f'{type(exc).__name__}: {exc}'}
    (directory / 'result.json').write_text(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == '__main__':
    main()
