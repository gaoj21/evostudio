import subprocess
import sys
import threading
import time
import uuid
import pytest
from backend.api import chat_control, chat_api, result_compute


def test_stop_before_request_arrives_prevents_model_and_tools(monkeypatch):
    request_id = uuid.uuid4().hex
    chat_api.stop_assistant('g', request_id)
    monkeypatch.setattr(chat_api, 'chat', lambda *args: pytest.fail('Stopped request executed'))
    result = chat_api.assistant('g', {'request_id': request_id})
    assert result['stopped'] is True
    assert chat_control.current.get() is None


def test_stop_is_scoped_to_graph():
    request_id = uuid.uuid4().hex
    own = chat_control.get_control('g', request_id, start=True)
    chat_api.stop_assistant('other', request_id)
    assert not own.event.is_set()
    own.finished = True


def test_stop_kills_blocking_worker(monkeypatch):
    processes = []
    original = subprocess.Popen
    def spawn(*args, **kwargs):
        process = original([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(chat_control.subprocess, 'Popen', spawn)
    control = chat_control.Control()
    token = chat_control.current.set(control)
    timer = threading.Timer(.2, control.event.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(chat_control.Cancelled):
            chat_control.worker('model', {'messages': []})
        assert time.monotonic() - started < 3
        assert processes[0].poll() is not None
    finally:
        timer.cancel()
        chat_control.current.reset(token)


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS compute sandbox')
def test_stop_interrupts_python_computation():
    control = chat_control.Control()
    token = chat_control.current.set(control)
    timer = threading.Timer(.2, control.event.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(chat_control.Cancelled):
            result_compute.compute([], 'while True: pass')
        assert time.monotonic() - started < 3
    finally:
        timer.cancel()
        chat_control.current.reset(token)


def test_stop_generation_never_publishes_a_graph(monkeypatch):
    control = chat_control.Control()
    token = chat_control.current.set(control)
    def worker(*args):
        control.event.wait(3)
        control.check()
    monkeypatch.setattr(chat_control, 'worker', worker)
    try:
        job_id = chat_api.start_generation({'tasks': [], 'edges': []}, 'A workflow to test stopping', 'stop-test-' + uuid.uuid4().hex)
    finally:
        chat_control.current.reset(token)
    control.event.set()
    for _ in range(50):
        if chat_api._jobs[job_id]['status'] != 'running':
            break
        time.sleep(.02)
    assert chat_api._jobs[job_id]['status'] == 'cancelled'
    assert chat_api._jobs[job_id]['graph'] is None


def test_dataset_worker_deadline_terminates_process(monkeypatch):
    processes = []
    original = subprocess.Popen
    def spawn(*args, **kwargs):
        process = original([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(chat_control.subprocess, 'Popen', spawn)
    with pytest.raises(TimeoutError, match='dataset worker exceeded'):
        chat_control.worker('dataset', {}, timeout=.2)
    assert processes[0].poll() is not None


def test_native_openmp_abort_is_reported_without_killing_parent(monkeypatch):
    original = subprocess.Popen
    def spawn(*args, **kwargs):
        return original([sys.executable, '-c', 'import os,sys; sys.stderr.write("OMP: Error #15: Initializing libomp.dylib\\n"); sys.stderr.flush(); os._exit(134)'], **kwargs)
    monkeypatch.setattr(chat_control.subprocess, 'Popen', spawn)
    with pytest.raises(RuntimeError, match='OpenMP runtime conflict.*backend is still running'):
        chat_control.worker('dataset', {}, timeout=5)
