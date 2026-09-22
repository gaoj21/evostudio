import pytest
from backend.features.data.dataset_interface import describe, arguments
from backend.features.data.torch_loader import execute_python

CODE='''from typing import Literal
from torch.utils.data import Dataset
class Rows(Dataset):
    def __len__(self): return 1
    def __getitem__(self, i): return {'count': self.count, 'split': self.split}
def build_dataset(resource, count: int, split: Literal['dev','test']='dev', enabled: bool=True):
    result=Rows()
    result.count=count if enabled else 0
    result.split=split
    return result
'''


def test_signature_generates_required_typed_inputs_without_executing_code():
    schema=describe(CODE+'\nraise RuntimeError("must not execute during analysis")')
    assert schema['inputs']==[
        {'name':'count','type':'int','required':True,'origin':'parameter'},
        {'name':'split','type':'str','required':False,'origin':'parameter','default':'dev','options':['dev','test']},
        {'name':'enabled','type':'bool','required':False,'origin':'parameter','default':True},
    ]


def test_interface_arguments_are_used_by_actual_pytorch_dataset():
    assert execute_python({'code':CODE,'resource':{},'config':{'count':3,'split':'test','enabled':False},'batch_size':2})==[{'count':0,'split':'test'}]
    assert execute_python({'code':CODE,'resource':{},'config':{'count':3},'batch_size':2})==[{'count':3,'split':'dev'}]


@pytest.mark.parametrize('values,message', [({},'Missing'),({'count':'3'},'must be int'),({'count':True},'must be int'),({'count':3,'split':'wrong'},'one of')])
def test_bad_form_values_fail_before_execution(values,message):
    with pytest.raises(ValueError,match=message): arguments(CODE,values)


def test_legacy_config_fields_and_dynamic_keys_are_explicit():
    code='''def build_dataset(resource, config):
    a = config['path']
    b = config.get('limit', 10)
    c = config.get('enabled', False)
    d = config['reference_inputs']
    e = config[a]
'''
    schema=describe(code)
    fields={f['name']:f for f in schema['inputs']}
    assert set(fields)=={'path','limit','enabled'}
    assert fields['path']['required']
    assert fields['limit']['default']==10
    assert fields['enabled']['type']=='bool'
    assert schema['warnings']


def test_multiple_classes_and_helpers_use_only_factory():
    code = CODE + '''
def unrelated(secret: str):
    raise RuntimeError("Must not call this")
class OtherDataset:
    pass
'''
    assert [f['name'] for f in describe(code)['inputs']] == ['count','split','enabled']
    assert execute_python({'code':code,'resource':{},'config':{'count':4},'batch_size':2}) == [{'count':4,'split':'dev'}]


@pytest.mark.parametrize('code', [
    'def build_dataset(): pass\ndef build_dataset(): pass',
    'async def build_dataset(): pass',
    'class build_dataset: pass',
])
def test_ambiguous_or_invalid_dataset_entrypoint_is_rejected(code):
    with pytest.raises(ValueError): describe(code)


def test_describe_distinguishes_declared_sampled_and_invalid_outputs():
    missing = describe(CODE)
    assert missing['output_schema_available'] is False
    assert 'Sample 5' in missing['output_status']
    declared = describe('OUTPUT_SCHEMA = [{"name":"value","type":"int"}]\n' + CODE)
    assert declared['output_schema_available'] is True
    assert declared['outputs'][0]['name'] == 'value'
    invalid = describe('OUTPUT_SCHEMA = [{"name":"value","type":"invalid"}]\n' + CODE)
    assert invalid['output_schema_available'] is False
    assert 'types' in invalid['output_schema_error']
    assert invalid['inputs'] == missing['inputs']  # sampling inputs still usable
