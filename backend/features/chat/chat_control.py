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


def worker_env():
    """The environment every Studio worker starts with.

    User code (a Dataset, an evaluator, a tool) may import several packages
    that each ship their own OpenMP runtime: torch, scikit-learn and faiss
    wheels all bundle a libomp. Two runtimes initializing in one process
    abort it with "OMP: Error #15", and running parallel regions in both can
    crash it outright. One OpenMP thread per worker keeps the runtimes from
    ever running concurrently, which is what makes the duplicate-runtime
    check safe to relax; Studio parallelizes across workers and records, not
    inside one. A value the user exported for any of these wins.

    What the server process inherited from the shell that pointed Python or
    the native loader somewhere else is removed: PYTHONPATH / PYTHONHOME /
    PYTHONSTARTUP (a conda or other environment's modules ahead of Studio's
    .venv), DYLD_* and LD_PRELOAD, and library-path entries inside a conda
    installation. A worker imports only what Studio's own Python has.
    """
    env = {key: value for key, value in os.environ.items()
           if key not in STRIPPED_ENV and not key.startswith('DYLD_')}
    if env.get('LD_LIBRARY_PATH'):
        kept = [entry for entry in env['LD_LIBRARY_PATH'].split(os.pathsep) if entry and not _foreign_python(entry)]
        if kept:
            env['LD_LIBRARY_PATH'] = os.pathsep.join(kept)
        else:
            del env['LD_LIBRARY_PATH']
    env['STUDIO_WORKER_PARENT'] = str(os.getpid())
    for key, value in {**OPENMP_ENV, **WORKER_DEFAULTS}.items():
        env.setdefault(key, value)
    return env


OPENMP_ENV = {'OMP_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1',
              'KMP_DUPLICATE_LIB_OK': 'TRUE'}
# Defaults (an exported value wins): no tokenizer thread pools racing a
# forked process, and no macOS Objective-C abort when a library forks.
WORKER_DEFAULTS = {'TOKENIZERS_PARALLELISM': 'false', 'OBJC_DISABLE_INITIALIZE_FORK_SAFETY': 'YES'}
STRIPPED_ENV = {'PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP', 'PYTHONEXECUTABLE', 'LD_PRELOAD'}


def _foreign_python(entry):
    """A library folder that belongs to a conda installation other than the
    Python running Studio."""
    path = os.path.realpath(entry)
    if path.startswith(os.path.realpath(sys.prefix) + os.sep):
        return False
    prefixes = [os.environ.get(key) for key in ('CONDA_PREFIX', 'CONDA_PREFIX_1', 'MAMBA_ROOT_PREFIX')]
    if any(prefix and path.startswith(os.path.realpath(prefix) + os.sep) for prefix in prefixes):
        return True
    return any(part in path.lower().split(os.sep) for part in ('anaconda3', 'miniconda3', 'miniforge3', 'mambaforge', 'anaconda', 'miniconda'))


def exit_diagnosis(kind, returncode, stderr_path):
    """Why a worker ended without writing its result, with the end of what
    it wrote to stderr (a native crash, a library's own message)."""
    try:
        with open(stderr_path, 'rb') as stderr:
            stderr.seek(max(0, stderr.seek(0, 2) - 8192))
            diagnostic = stderr.read().decode('utf-8', errors='replace')
    except OSError:
        diagnostic = ''
    if 'OMP: Error #15' in diagnostic:
        return f'{kind} worker stopped: OpenMP runtime conflict (OMP Error #15): two copies of the OpenMP runtime were loaded. Studio starts workers with KMP_DUPLICATE_LIB_OK=TRUE and one OpenMP thread; this environment overrides that ({ {k: os.environ.get(k) for k in OPENMP_ENV if k in os.environ} }). Unset those variables and restart Studio. The backend is still running.'
    tail = diagnostic.strip()[-2000:]
    output = f'\nLast worker output (stderr):\n{tail}' if tail else ''
    if returncode == -9:
        return f'{kind} worker was forcibly killed (SIGKILL / -9). Memory pressure is one possible cause, not confirmed by the exit code. For DataLoader interfaces, declare OUTPUT_SCHEMA to avoid executing the Dataset. The backend is still running.{output}'
    reason = ''
    if returncode is not None and returncode < 0:
        try:
            reason = f' ({signal.Signals(-returncode).name}, a crash in native code or a signal)'
        except ValueError:
            pass
    return f'{kind} worker exited without a result (exit code {returncode}{reason}); the backend is still running.{output}'


def worker(kind, payload, on_stage=None, on_record=None, timeout=None):
    check()
    control = current.get()
    with tempfile.TemporaryDirectory(prefix='assistant-worker-') as directory:
        root = Path(directory)
        (root / 'input.json').write_text(json.dumps(payload, ensure_ascii=False))
        with (root / 'stderr.log').open('wb') as stderr:
            process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('chat_worker.py')), kind, directory],
                                       stdout=subprocess.DEVNULL, stderr=stderr, start_new_session=True,
                                       env=worker_env())
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
                    stage_path = root / 'stage.txt'
                    stage = stage_path.read_text()[:500] if kind == 'dataset' and stage_path.exists() else None
                    detail = f'. Last stage: {stage}. Check initialization/preprocessing or increase Sample time limit in Input.' if stage else ''
                    raise TimeoutError(f'{kind} worker exceeded {timeout} seconds{detail}')
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
                raise RuntimeError(exit_diagnosis(kind, process.returncode, root / 'stderr.log'))
            result = json.loads((root / 'result.json').read_text())
            if 'error' in result:
                raise RuntimeError(result['error'])
            return result['value']
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
