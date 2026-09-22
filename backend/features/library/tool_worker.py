"""The isolated process one custom toolkit runs in (see tool_sessions.py).

Reads one JSON request per line on stdin and answers one JSON line on the
original stdout. What the tool prints goes nowhere near that channel: it is
kept, and an error carries it, together with the line of the tool's own code
that failed (backend/features/user_code.py). Exits when stdin closes or the
process that started it goes away.
"""
import contextlib
import json
import os
import sys
import threading
import time
from pathlib import Path

# Not this file's own folder (Python puts a script's directory first): its
# modules would shadow the tool's imports. The repository goes last on the
# path: `backend.features...` must import, but nothing in it may shadow a
# module the tool's own code imports.
if sys.path and Path(sys.path[0] or '.').resolve() == Path(__file__).resolve().parent:
    sys.path.pop(0)
_REPO = str(Path(__file__).resolve().parents[3])
if _REPO not in sys.path:
    sys.path.append(_REPO)


def _exit_with_parent():
    parent = int(os.environ.get('STUDIO_WORKER_PARENT') or os.getppid())

    def watch():
        while True:
            time.sleep(1)
            if os.getppid() != parent:
                os._exit(3)
    threading.Thread(target=watch, daemon=True).start()


def _handle(request, state):
    from backend.features.library import tool_runtime as runtime
    op = request.get('op')
    if op == 'load':
        state.clear()
        state.update(request)
        if request.get('dependency_dir'):
            sys.path.insert(0, request['dependency_dir'])
        if request.get('package_dir'):
            # An uploaded folder: its files sit beside its code.
            os.chdir(request['package_dir'])
        state['namespace'], state['filename'] = runtime.load_entry(
            request['code'], request.get('package_dir'), request.get('entry_file'),
            module_name=f"studio_tool_{request.get('name') or 'toolkit'}")
        if request.get('kind') == 'class':
            state['instance'] = runtime.build_instance(
                state['namespace'], request.get('target'), request.get('configuration'),
                request.get('config'))
        return None
    if 'namespace' not in state:
        raise RuntimeError('The toolkit was not loaded.')
    namespace, kind = state['namespace'], state.get('kind')
    if op == 'discover':
        return runtime.discover(namespace, state.get('target'), state.get('configuration'),
                                state.get('config'),
                                runtime.owned_by(state['filename'], state.get('package_dir')))
    if op != 'call':
        raise ValueError(f'Unknown request {op!r}')
    name, args = request['tool'], request.get('args') or {}
    if not isinstance(args, dict):
        raise ValueError('Tool arguments must be an object.')
    if kind == 'class':
        return runtime.call_method(state['instance'], name, args)
    if kind == 'factory':
        # The older class contract: build_tool + INPUT_SCHEMA / run(inputs).
        from backend.features.library.python_tool import execute
        return execute(namespace, state['code'], state.get('config') or {}, args)
    entry = namespace.get(name)
    if entry is None and state.get('legacy_run'):
        entry = namespace.get('run')        # the single-function shape of old
    if not callable(entry):
        raise ValueError(f'code does not define a callable {name!r}')
    return entry(**args)


def main():
    _exit_with_parent()
    # The protocol keeps the real stdout to itself; fd 1 now goes to stderr,
    # so a native library writing to it cannot corrupt a reply.
    channel = os.fdopen(os.dup(1), 'w', encoding='utf-8')
    os.dup2(2, 1)
    from backend.features.user_code import TailBuffer, explain
    state = {}
    for line in sys.stdin:
        if not line.strip():
            continue
        output = TailBuffer()
        request = {}
        try:
            request = json.loads(line)
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                value = _handle(request, state)
            reply = json.dumps({'ok': True, 'result': value, 'logs': output.getvalue()},
                               ensure_ascii=False, default=str)
        except (Exception, SystemExit) as exc:
            code = state.get('code') or request.get('code') or ''
            filename = state.get('filename') or _entry_filename(request)
            root = state.get('package_dir') or request.get('package_dir') or None
            reply = json.dumps({'ok': False, 'error': explain(exc, code, output.getvalue(), filename, 'Tool code', root),
                                'logs': output.getvalue()}, ensure_ascii=False, default=str)
            if request.get('op') == 'load':
                state.clear()
        channel.write(reply + '\n')
        channel.flush()


def _entry_filename(request):
    """The filename a failed load compiled its code under."""
    from backend.features.library.tool_runtime import ENTRY_FILENAME
    if request.get('package_dir'):
        return str(Path(request['package_dir']).resolve() / (request.get('entry_file') or 'tools.py'))
    return ENTRY_FILENAME


if __name__ == '__main__':
    main()
