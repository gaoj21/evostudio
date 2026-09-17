"""Dependency installation and example-based verification of generated tools."""
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

from backend.api import custom_tools


def configuration(spec):
    requirements = spec.get('requirements') or []
    examples = spec.get('tests') or []
    if not isinstance(requirements, list) or len(requirements) > 30 or any(
        not isinstance(r, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?(?:(?:==|>=|<=|~=|>|<)[A-Za-z0-9.*+!-]+(?:,(?:>=|<=|>|<)[A-Za-z0-9.*+!-]+)*)?', r)
        for r in requirements
    ):
        raise custom_tools.CustomToolError('requirements must be package names with optional version constraints, not URLs or pip options.')
    if not isinstance(examples, list) or len(examples) > 20:
        raise custom_tools.CustomToolError('Provide at most 20 test examples.')
    names = {t['name'] for t in spec['tools']}
    for example in examples:
        if not isinstance(example, dict) or example.get('tool') not in names or not isinstance(example.get('args'), dict):
            raise custom_tools.CustomToolError('Each test needs an exported tool name and an args object.')
    return requirements, examples


def dependency_path(spec):
    digest = hashlib.sha256(json.dumps(spec.get('requirements') or [], sort_keys=True).encode()).hexdigest()[:16]
    return custom_tools.TOOLS_DIR / '_dependencies' / spec['name'] / digest


def verify(spec):
    requirements, examples = configuration(spec)
    report = {'status': 'unverified', 'code_sha256': hashlib.sha256(spec['code'].encode()).hexdigest(),
              'requirements': requirements, 'tests': [], 'scope': 'Provided examples only; not a guarantee for all inputs.'}
    if requirements:
        target = dependency_path(spec)
        marker = target / '.installed'
        if not marker.exists():
            target.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',
                                   '--no-input', '--upgrade', '--target', str(target), *requirements],
                                  capture_output=True, text=True, timeout=180)
            if proc.returncode:
                return {**report, 'status':'failed', 'error':'Dependency installation failed', 'details':(proc.stdout + proc.stderr)[-2000:]}
            marker.write_text('ok')
    for example in examples:
        # Calls the same subprocess execution path used by workflow tools.
        result = custom_tools.run_custom_tool(example['tool'], example['args'])
        status = 'failed' if 'error' in result else ('passed' if 'expected' in example and result['result'] == example['expected'] else 'smoke_passed' if 'expected' not in example else 'failed')
        report['tests'].append({'tool':example['tool'], 'status':status, **result,
                                **({'expected':example['expected']} if 'expected' in example else {})})
    covered = {r['tool'] for r in report['tests'] if r['status'] == 'passed'}
    report['untested_tools'] = sorted({t['name'] for t in spec['tools']} - covered)
    if any(r['status'] == 'failed' for r in report['tests']):
        report['status'] = 'failed'
    elif not report['untested_tools']:
        report['status'] = 'verified'
    return report


def verify_saved(name):
    custom_tools._validate_name(name, [])
    spec = custom_tools._read_spec(name)
    if not spec:
        raise custom_tools.CustomToolError('Tool not found.')
    from backend.features.chat import chat_control
    try:
        result = chat_control.worker('verify_tool', {'spec':spec}) if chat_control.current.get() else verify(spec)
    except Exception as exc:
        result = {'status':'failed', 'error':str(exc)}
    # Do not attach a stale report to a tool edited while tests were running.
    with custom_tools._lock:
        current = custom_tools._read_spec(name)
        if current != spec:
            raise custom_tools.CustomToolError('Tool changed during verification; verify the current version again.')
        current['verification'] = result
        custom_tools._path(name).write_text(json.dumps(current, ensure_ascii=False, indent=2))
    return result
