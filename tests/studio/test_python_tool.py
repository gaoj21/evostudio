import pytest
from fastapi.testclient import TestClient
from backend.api import custom_tools, tools_registry
from backend.features.library.python_tool import interface

CODE='''"""Multiply numeric inputs."""
INPUT_SCHEMA = [{"name":"value","type":"float"}]
OUTPUT_SCHEMA = [{"name":"total","type":"float"}]
def helper(x, factor): return x * factor
class Multiply:
    def __init__(self, factor): self.factor = factor
    def run(self, inputs): return {"total":helper(inputs["value"],self.factor)}
def build_tool(factor: float = 2.0): return Multiply(factor)
'''

@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(custom_tools,'TOOLS_DIR',tmp_path)
    return tmp_path


def test_static_schema_does_not_execute_code():
    result=interface(CODE+'\nraise RuntimeError("Do not execute")')
    assert [f['name'] for f in result['configuration']['inputs']]==['factor']
    assert result['inputs'][0]['name']=='value'


def test_factory_registered_once_and_callable_by_registry_and_canvas(library):
    spec=custom_tools.validate_spec({'name':'multiply_demo','code':CODE,'config':{'factor':3}},[])
    custom_tools.save_custom_tool(spec)
    assert [t['name'] for t in spec['tools']]==['multiply_demo']
    assert custom_tools.run_custom_tool('multiply_demo',{'value':4})=={'result':{'total':12}}
    assert tools_registry.call_tool('multiply_demo',{'value':5})=={'total':15}
    from backend.features.workflow.runner import _tool_outputs
    assert _tool_outputs({'tool':'multiply_demo','outputs':[{'name':'total'}]}, {'total':12})=={'total':12}
    assert 'must be float' in custom_tools.run_custom_tool('multiply_demo',{'value':'bad'})['error']
    assert 'Missing Input' in custom_tools.run_custom_tool('multiply_demo',{})['error']


def test_output_contract_is_enforced(library):
    code=CODE.replace('return {"total":helper(inputs["value"],self.factor)}','return {"total":"wrong"}')
    spec=custom_tools.validate_spec({'name':'bad_output','code':code},[])
    custom_tools.save_custom_tool(spec)
    assert 'Output field total must be float' in custom_tools.run_custom_tool('bad_output',{'value':1})['error']


def test_preview_does_not_save_tool_and_configuration_is_separate(library):
    from backend.api.app import app
    client=TestClient(app)
    result=client.post('/api/tools/custom/interface',json={'code':CODE})
    assert result.status_code==200,result.text
    result=client.post('/api/tools/custom/preview',json={'name':'draft_multiply','code':CODE,'config':{'factor':4},'args':{'value':3}})
    assert result.status_code==200,result.text
    assert result.json()['result']=={'total':12}
    assert custom_tools.list_custom_tools()==[]


def test_framework_adapter_optional_inputs_and_output(library):
    code=CODE.replace('"type":"float"}]','"type":"float","required":False}]',1).replace('inputs["value"]','inputs.get("value", 5)')
    spec=custom_tools.validate_spec({'name':'optional_demo','code':code},[])
    custom_tools.save_custom_tool(spec)
    toolkit=custom_tools.make_toolkit(spec)
    tool=toolkit.tools[0]
    assert tool()=={'result':{'total':10}}


def test_factory_runs_in_real_workflow(library):
    from backend.api import runner
    spec=custom_tools.validate_spec({'name':'workflow_multiply','code':CODE,'config':{'factor':3}},[])
    custom_tools.save_custom_tool(spec)
    graph={'id':'factory-tool-regression','name':'Factory test','goal':'Multiply','flow_version':2,
           'tasks':[{'name':'multiply','kind':'tool','tool':'workflow_multiply',
                     'inputs':[{'name':'value','type':'float','required':True}],
                     'outputs':[{'name':'total','type':'float','required':True}]}], 'edges':[]}
    rid=runner.start_run(graph,{'value':4},background=False)
    run=runner.get_run(rid)
    assert run['status']=='success',run.get('error')
    assert run['node_outputs']['multiply']=={'total':12}
