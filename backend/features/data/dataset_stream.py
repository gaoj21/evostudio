"""Pull one PyTorch batch at a time across a process boundary, with backpressure."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from backend.api.sources import SourceError
from . import data_resources, dataloaders


def chunks(config, cancelled=lambda: False, on_info=None):
    dataloaders.validate(config)
    if config.get('loader') != 'python': raise SourceError('Streaming needs a Python Dataset.')
    if any(config.get(k) for k in ('transform_tool','field_mapping','group_by','order_by')) or config.get('input_mode') == 'reference':
        raise SourceError('For streaming, implement preprocessing and ordering inside your Dataset and use per-record inputs.')
    return read(config, cancelled, on_info)


def read(config, cancelled=lambda: False, on_info=None):
    """The Dataset's selected records, one batch at a time, from one worker.

    The Dataset is built once for the whole read. Its initialization (imports,
    loading a model, build_dataset) gets the Input's "Sample time limit",
    which covers exactly that; every batch after it gets "time allowed per
    batch", however many batches there are.
    """
    resource=data_resources.load(config['resource_id'])
    import fnmatch
    resource={**resource,'root':str(data_resources.path_for(resource['id'])/'files'),
              'files':[r for r in resource['files'] if fnmatch.fnmatch(r['path'],config.get('file_pattern') or '*')]}
    payload={'code':config['code'],'resource':resource,'config':{**(config.get('reader_config') or {}),'reference_inputs':config.get('reference_inputs') or {}},
             'batch_size':config.get('read_batch_size',100),'offset':config.get('offset',0),'record_limit':config.get('n',0)}
    from backend.features.chat import chat_worker, chat_control
    with tempfile.TemporaryDirectory(prefix='dataset-stream-') as directory:
        root=Path(directory);(root/'input.json').write_text(json.dumps(payload, ensure_ascii=False))
        with (root/'stderr.log').open('wb') as log:
            # The worker exits by itself if this process disappears (restart,
            # crash): it would otherwise wait for its next batch forever.
            process=subprocess.Popen([sys.executable,chat_worker.__file__,'dataset_stream',directory],stdout=subprocess.DEVNULL,stderr=log,start_new_session=True,
                                     env=chat_control.worker_env())
        # The DataLoader's own "time allowed per batch", as in a full read.
        allowed=dataloaders.batch_timeout(config)
        initialization=dataloaders.preview_timeout(config)
        deadline=time.monotonic()+initialization
        info_read = False

        def stage():
            try:
                return (root/'stage.txt').read_text()[:500]
            except OSError:
                return ''
        try:
            while True:
                if cancelled(): return
                info = root/'info.json'
                if not info_read and info.exists():
                    info_read = True
                    # Initialization is over: the first batch gets its own time.
                    deadline=time.monotonic()+allowed
                    if on_info is not None: on_info(json.loads(info.read_text()))
                chunk=root/'chunk.json'
                if chunk.exists():
                    rows=dataloaders._object_rows(json.loads(chunk.read_text()))
                    yield rows
                    chunk.unlink(missing_ok=True)
                    deadline=time.monotonic()+allowed
                    continue
                if process.poll() is not None:
                    result=root/'result.json'
                    if not result.exists():
                        raise SourceError(chat_control.exit_diagnosis('Dataset', process.returncode, root/'stderr.log'))
                    outcome=json.loads(result.read_text())
                    if 'error' in outcome: raise SourceError(outcome['error'])
                    return
                if time.monotonic()>deadline:
                    where = f' Last stage: {stage()}.' if stage() else ''
                    if not info_read:
                        raise SourceError(f'Dataset initialization (imports, build_dataset, loading models) did not finish within {initialization} seconds.{where} Raise "Sample time limit" in the Input\'s Advanced settings if it needs longer.')
                    raise SourceError(f'Dataset did not produce a batch within {allowed} seconds.{where} Keep per-item work bounded, or raise the time allowed per batch.')
                time.sleep(.05)
        finally:
            if process.poll() is None:
                os.killpg(process.pid,signal.SIGKILL);process.wait()
