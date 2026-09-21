"""Isolated blocking calls so Stop can terminate sockets and model retries."""
import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root.parent))
sys.path.insert(0, str(root))


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
    directory = Path(directory)
    _exit_with_parent(directory)
    payload = json.loads((directory / 'input.json').read_text())
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
            from backend.features.data.torch_loader import execute_python
            def emit(rows):
                temporary = directory / 'chunk.tmp'
                temporary.write_text(json.dumps(rows, ensure_ascii=False, allow_nan=False))
                temporary.replace(directory / 'chunk.json')
                while (directory / 'chunk.json').exists(): time.sleep(.05)
            value = execute_python(payload, emit=emit)
        elif kind == 'dataset_batches':
            from backend.features.data.torch_loader import RecordDataset, batches
            value = list(batches(RecordDataset(payload['records']), payload['batch_size']))
        elif kind == 'dataset':
            from backend.features.data.torch_loader import execute_python
            value = execute_python(payload)
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
