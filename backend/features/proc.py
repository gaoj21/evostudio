"""Worker processes and everything they start, on POSIX and on Windows.

A worker runs in its own process group (a new session on POSIX, a new
process group on Windows) so that stopping it also stops whatever the user's
code started. `os.killpg` and `signal.SIGKILL` do not exist on Windows; there
the whole tree is ended with `taskkill /T /F`.
"""
import contextlib
import os
import signal
import subprocess
import sys

# Not defined by `subprocess` outside Windows; the documented values.
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x00000200)
CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)


def _windows():
    return sys.platform == 'win32'


def popen_group_kwargs():
    """Keyword arguments for `subprocess.Popen` that start the child in a
    process group of its own."""
    if _windows():
        return {'creationflags': CREATE_NEW_PROCESS_GROUP}
    return {'start_new_session': True}


def kill_tree(process):
    """Kill `process` (a Popen started with `popen_group_kwargs()`) and the
    processes it started. On POSIX an OSError from `killpg` (the group is
    already gone) propagates, as `os.killpg` itself would."""
    if _windows():
        subprocess.run(['taskkill', '/T', '/F', '/PID', str(process.pid)],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=CREATE_NO_WINDOW, check=False)
        if process.poll() is None:
            with contextlib.suppress(OSError):
                process.kill()
        return
    os.killpg(process.pid, signal.SIGKILL)
