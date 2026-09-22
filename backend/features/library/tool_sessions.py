"""Isolated processes custom tools run in.

A tool never runs in the API process. Each call gets a fresh worker process
(tool_worker.py), as it always has -- except that inside a run scope a class
toolkit keeps one worker, and so one instance of its class, for the whole run:
what it set up or remembered in one call is there for the next. The scope
ends with the run and its workers with it.

A call that takes longer than its time limit kills the worker; a later call
in the same scope starts a new one.
"""
import contextlib
import hashlib
import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
from contextvars import ContextVar
from pathlib import Path

WORKER = str(Path(__file__).with_name('tool_worker.py'))


class ToolError(Exception):
    """The tool (or loading it) failed; the message says where and why."""

    def __init__(self, message, logs=''):
        super().__init__(message)
        self.logs = logs


class ToolTimeout(ToolError):
    """The tool did not answer within its time limit."""


class ToolProcess:
    """One worker holding one loaded toolkit."""

    def __init__(self, load, timeout):
        self.stderr = tempfile.TemporaryFile()
        self.process = subprocess.Popen(
            [sys.executable, WORKER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self.stderr, start_new_session=True, text=True, encoding='utf-8',
            env={**os.environ, 'STUDIO_WORKER_PARENT': str(os.getpid())})
        self.replies = queue.Queue()
        self.lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self.request({'op': 'load', **load}, timeout)
        except BaseException:
            self.close()
            raise

    def _read(self):
        for line in self.process.stdout:
            self.replies.put(line)
        self.replies.put(None)

    def _stopped(self):
        self.process.wait()
        self.stderr.seek(0, 2)
        self.stderr.seek(max(0, self.stderr.tell() - 2000))
        tail = self.stderr.read().decode('utf-8', 'replace').strip()
        code = self.process.returncode
        detail = ' (killed: SIGKILL, possibly memory pressure)' if code == -9 else ''
        return (f'Tool process exited without answering (exit code {code}){detail}.'
                + (f'\n{tail}' if tail else ''))

    def request(self, message, timeout):
        """Send one request; returns the reply {result, logs}."""
        with self.lock:
            if self.process.poll() is not None:
                raise ToolError(self._stopped())
            try:
                self.process.stdin.write(json.dumps(message, ensure_ascii=False, default=str) + '\n')
                self.process.stdin.flush()
            except (BrokenPipeError, OSError):
                raise ToolError(self._stopped())
            try:
                line = self.replies.get(timeout=timeout)
            except queue.Empty:
                self.close()
                raise ToolTimeout(f'timed out after {timeout}s')
            if line is None:
                raise ToolError(self._stopped())
            reply = json.loads(line)
            if not reply.get('ok'):
                raise ToolError(reply.get('error') or 'The tool failed.', reply.get('logs') or '')
            return reply

    @property
    def alive(self):
        return self.process.poll() is None

    def kill(self):
        """End the worker now, and with it whatever the tool started.

        `close` asks first and waits a couple of seconds for an answer, which
        is right at the end of a run. A stop has nothing to wait for: the call
        in flight is the thing being stopped.
        """
        with contextlib.suppress(OSError):
            os.killpg(self.process.pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            self.stderr.close()

    def close(self):
        if self.process.poll() is None:
            with contextlib.suppress(OSError):
                self.process.stdin.close()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait()
        else:
            # Whatever the tool itself started goes too.
            with contextlib.suppress(OSError):
                os.killpg(self.process.pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            self.stderr.close()


def once(load, request, timeout):
    """Load a toolkit in a fresh worker, make one request, stop the worker.

    The worker is registered with the run's scope while the call lasts, even
    though it is not kept afterwards: a stop has to be able to reach the call
    in flight, and the caller is blocked waiting for it.
    """
    worker = ToolProcess(load, timeout)
    active = current()
    if active is not None:
        active.track(worker)
    try:
        return worker.request(request, timeout)
    finally:
        if active is not None:
            active.release(worker)
        worker.close()


class Scope:
    """Workers kept for the length of one run, one per loaded toolkit."""

    def __init__(self):
        self.workers = {}
        # Workers of one-off calls, held only while the call is in flight.
        self.transient = set()
        self.lock = threading.Lock()

    def track(self, worker):
        with self.lock:
            self.transient.add(worker)

    def release(self, worker):
        with self.lock:
            self.transient.discard(worker)

    def request(self, load, request, timeout):
        key = hashlib.sha256(json.dumps(load, sort_keys=True, default=str).encode()).hexdigest()
        with self.lock:
            worker = self.workers.get(key)
            if worker is None or not worker.alive:
                # A worker killed by a time limit is replaced: the instance
                # starts over, which the error of the call that hit the limit
                # has already said.
                worker = self.workers[key] = ToolProcess(load, timeout)
        return worker.request(request, timeout)

    def close(self):
        with self.lock:
            workers, self.workers = list(self.workers.values()), {}
        for worker in workers:
            worker.close()

    def kill(self):
        """Stop every worker of this scope at once, for a run being stopped.

        The call waiting on a killed worker comes back as a failed tool call;
        the run it belongs to is already being stopped and reads as stopped.
        """
        with self.lock:
            workers = list(self.workers.values()) + list(self.transient)
            self.workers, self.transient = {}, set()
        for worker in workers:
            worker.kill()


_current = ContextVar('studio_tool_scope', default=None)


def current():
    """The run scope in effect, or None."""
    return _current.get()


@contextlib.contextmanager
def scope():
    """Keep class toolkits' instances for the length of the enclosed run."""
    active = Scope()
    token = _current.set(active)
    try:
        yield active
    finally:
        _current.reset(token)
        active.close()
