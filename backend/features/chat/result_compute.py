"""Run result-analysis Python in an OS sandbox, never in the API process."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from backend.api import chat_control


def compute(rows, code, timeout=20):
    chat_control.check()
    if not isinstance(code, str) or not code.strip() or len(code) > 30000:
        raise ValueError('Provide Python code of at most 30000 characters.')
    if sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').exists():
        raise ValueError('The result computation sandbox is unavailable on this host.')
    with tempfile.TemporaryDirectory(prefix='result-analysis-') as directory:
        root = Path(directory).resolve()
        (root / 'records.json').write_text(json.dumps(rows, ensure_ascii=False, default=str))
        (root / 'analysis.py').write_text(code)
        (root / 'run.py').write_text('''import json, resource
resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
resource.setrlimit(resource.RLIMIT_FSIZE, (1048576, 1048576))
resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
with open('records.json') as source:
    records = json.load(source)
with open('analysis.py') as source:
    code = source.read()
exec(compile(code, 'analysis.py', 'exec'), {'records': records, '__name__': '__main__'})
''')
        # Only runtime libraries and this disposable dataset are readable. No
        # project files, credentials, network, or child processes are allowed.
        read_roots = {str(root), sys.base_prefix, sys.prefix, '/System', '/usr/lib', '/usr/share', '/private/var/db/dyld', '/private/var/db/timezone'}
        executable = str(Path(sys.executable).resolve())
        profile = '\n'.join([
            '(version 1)', '(deny default)',
            '(allow process-exec)',
            '(allow ipc-posix-shm-read-data (ipc-posix-name "apple.shm.notification_center"))', '(allow sysctl-read)', '(allow mach-lookup)', '(allow process-info*)', '(allow signal (target self))',
            '(allow file-read-metadata)', '(allow file-read* (literal "/"))',
            '(allow file-read* ' + ' '.join(f'(subpath {json.dumps(path)})' for path in read_roots) + ' (literal "/dev/null") (literal "/dev/urandom") (literal "/dev/random"))',
            f'(allow file-write* (subpath {json.dumps(str(root))}) (literal "/dev/null"))',
        ])
        with open(root / 'stdout.txt', 'wb') as stdout, open(root / 'stderr.txt', 'wb') as stderr:
            process = subprocess.Popen(['/usr/bin/sandbox-exec', '-p', profile, executable, '-I', str(root / 'run.py')],
                cwd=root, env={'PATH': '/usr/bin:/bin', 'HOME': str(root), 'TMPDIR': str(root), 'PYTHONDONTWRITEBYTECODE': '1', 'OPENBLAS_NUM_THREADS': '1'},
                stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True)
            timed_out = False
            deadline = time.monotonic() + timeout
            try:
                while process.poll() is None:
                    chat_control.check()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    try:
                        process.wait(timeout=min(.1, remaining))
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        output = (root / 'stdout.txt').read_text(errors='replace')
        errors = (root / 'stderr.txt').read_text(errors='replace')
        return {'status': 'timeout' if timed_out else 'success' if process.returncode == 0 else 'error',
                'record_count': len(rows), 'stdout': output[:24000], 'stderr': errors[:6000],
                'output_truncated': len(output) > 24000 or len(errors) > 6000,
                'exit_code': process.returncode}
