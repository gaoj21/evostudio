import sys
from pathlib import Path
import pytest
from backend.api.result_compute import compute

pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='macOS sandbox backend')


def test_sandbox_blocks_project_reads_network_and_processes():
    project_file = str(Path(__file__).resolve())
    for code in [f'open({project_file!r}).read()', 'import socket; socket.socket().connect(("127.0.0.1",8000))', 'import subprocess; subprocess.run(["/bin/echo","child"])']:
        result = compute([], code)
        assert result['status'] == 'error'
        assert 'PermissionError' in result['stderr']


def test_timeout_and_output_limit():
    assert compute([], 'while True: pass', timeout=.2)['status'] == 'timeout'
    assert compute([], 'print("x" * 25000)')['output_truncated'] is True


def test_arbitrary_formula_and_isolation():
    result = compute([{'v': 2}, {'v': 4}], 'import statistics\nprint(statistics.mean(r["v"]**2 for r in records))\nrecords.clear()')
    assert result['stdout'].strip() == '10'
    assert compute([{'v': 2}], 'print(len(records))')['stdout'].strip() == '1'
