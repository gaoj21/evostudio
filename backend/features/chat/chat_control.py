"""Cancellation shared by assistant requests, model workers and computation."""
from contextvars import ContextVar
import json
from pathlib import Path
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time


class Cancelled(BaseException):
    pass


current = ContextVar('assistant_cancellation', default=None)
_lock = threading.Lock()
_requests = {}


class Control:
    def __init__(self):
        self.event = threading.Event()
        self.updated = time.monotonic()
        self.finished = False
        self.background = False
        self.started = False

    def check(self):
        if self.event.is_set():
            raise Cancelled()


def get_control(graph_id, request_id, start=False):
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', request_id):
        raise ValueError('Invalid assistant request ID')
    with _lock:
        now = time.monotonic()
        for key, control in list(_requests.items()):
            if (control.finished or not control.started) and now - control.updated > 600:
                del _requests[key]
        control = _requests.setdefault((graph_id, request_id), Control())
        if start:
            if control.started:
                raise ValueError('Assistant request ID has already been used')
            control.started = True
        control.updated = now
        return control


def check():
    control = current.get()
    if control:
        control.check()


def worker(kind, payload, on_stage=None, on_record=None, timeout=None):
    check()
    control = current.get()
    with tempfile.TemporaryDirectory(prefix='assistant-worker-') as directory:
        root = Path(directory)
        (root / 'input.json').write_text(json.dumps(payload, ensure_ascii=False))
        with (root / 'stderr.log').open('wb') as stderr:
            process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('chat_worker.py')), kind, directory],
                                       stdout=subprocess.DEVNULL, stderr=stderr, start_new_session=True)
        started = time.monotonic()
        last_stage = None
        offset = 0
        def drain():
            nonlocal offset
            path = root / 'records.jsonl'
            if not on_record or not path.exists():
                return
            with path.open() as stream:
                stream.seek(offset)
                while True:
                    line = stream.readline()
                    if not line.endswith('\n'):
                        break
                    on_record(json.loads(line))
                    offset = stream.tell()
        try:
            while process.poll() is None:
                if timeout is not None and time.monotonic() - started > timeout:
                    raise TimeoutError(f'{kind} worker exceeded {timeout} seconds')
                drain()
                if control:
                    control.check()
                if on_stage and (root / 'stage.txt').exists():
                    stage = (root / 'stage.txt').read_text()
                    if stage and stage != last_stage:
                        on_stage(stage)
                        last_stage = stage
                try:
                    process.wait(timeout=.1)
                except subprocess.TimeoutExpired:
                    pass
            check()
            drain()
            if not (root / 'result.json').exists():
                with (root / 'stderr.log').open('rb') as stderr:
                    stderr.seek(max(0, stderr.seek(0, 2) - 8192))
                    diagnostic = stderr.read().decode('utf-8', errors='replace')
                if 'OMP: Error #15' in diagnostic:
                    raise RuntimeError(f'{kind} worker stopped: OpenMP runtime conflict (OMP Error #15). Multiple libomp/libiomp copies were loaded. Use a clean Python environment with compatible native packages; do not enable KMP_DUPLICATE_LIB_OK. The backend is still running.')
                if process.returncode == -9:
                    raise RuntimeError(f'{kind} worker was forcibly killed (SIGKILL / -9). Memory pressure is one possible cause, not confirmed by the exit code. For DataLoader interfaces, declare OUTPUT_SCHEMA to avoid executing the Dataset. The backend is still running.')
                raise RuntimeError(f'{kind} worker exited without a result (exit code {process.returncode}); the backend is still running.')
            result = json.loads((root / 'result.json').read_text())
            if 'error' in result:
                raise RuntimeError(result['error'])
            return result['value']
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
