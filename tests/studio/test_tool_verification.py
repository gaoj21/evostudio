import json
import subprocess

import pytest
from backend.api import custom_tools, chat_api
from backend.features.library import tool_verification as verification

CODE = '''def count(text: str) -> dict:
    """Count words.

    Args:
        text: Input text.
    """
    return {"count": len(text.split())}
'''


def spec(tests=None, **extra):
    return custom_tools.validate_spec({'name':'counter','code':CODE,'tests':tests or [], **extra}, [])


def test_chat_creation_executes_examples_and_returns_evidence():
    result = chat_api.apply_operations({'tasks':[], 'edges':[]}, [{'op':'create_tool','spec':spec([
        {'tool':'count','args':{'text':'hello world'},'expected':{'count':2}},
        {'tool':'count','args':{'text':''},'expected':{'count':0}},
    ])}])
    check = result['observations'][0]['result']['verification']
    assert check['status'] == 'verified'
    assert check['tests'][0]['result'] == {'count':2}
    assert custom_tools._read_spec('counter')['verification'] == check


@pytest.mark.parametrize('tests,status', [([], 'unverified'),
    ([{'tool':'count','args':{'text':'hello'}}], 'unverified'),
    ([{'tool':'count','args':{'text':'hello'},'expected':{'count':9}}], 'failed'),
    ([{'tool':'count','args':{'text':None},'expected':{'count':0}}], 'failed')])
def test_verification_does_not_claim_unproven_correctness(tests, status):
    custom_tools.save_custom_tool(spec(tests))
    assert verification.verify_saved('counter')['status'] == status


def test_dependencies_are_isolated_and_used_by_runtime(monkeypatch):
    code = CODE.replace('return {"count": len(text.split())}', 'import fake_dependency\n    return {"count": fake_dependency.VALUE}')
    data = spec([{'tool':'count','args':{'text':'hello'},'expected':{'count':7}}], code=code, requirements=['fake-dependency==1.0'])
    custom_tools.save_custom_tool(data)
    real_run = subprocess.run
    def run(command, **kwargs):
        if 'pip' in command:
            target = verification.dependency_path(data)
            assert command[command.index('--target')+1] == str(target)
            (target / 'fake_dependency.py').write_text('VALUE = 7')
            return subprocess.CompletedProcess(command, 0, '', '')
        return real_run(command, **kwargs)
    monkeypatch.setattr(subprocess, 'run', run)
    assert verification.verify_saved('counter')['status'] == 'verified'
    assert custom_tools.run_custom_tool('count', {'text':'later'}) == {'result':{'count':7}}


def test_missing_import_surfaces_as_failed_example():
    custom_tools.save_custom_tool(spec([{'tool':'count','args':{'text':'a'},'expected':{'count':1}}], code='import definitely_missing_example_dependency\n'+CODE))
    report = verification.verify_saved('counter')
    assert report['status'] == 'failed'
    assert 'ModuleNotFoundError' in report['tests'][0]['error']


def test_reject_pip_flags_and_invalid_tests():
    with pytest.raises(custom_tools.CustomToolError):
        spec(requirements=['--upgrade'])
    with pytest.raises(custom_tools.CustomToolError):
        spec([{'tool':'unknown','args':{}}])
