"""Static interfaces and execution contract for class-based Python tools."""
import ast
import inspect
import keyword
import json
from backend.features.data.dataset_interface import describe, arguments, declared_outputs

JSON_TYPES = {'str':'string','int':'integer','float':'number','bool':'boolean','dict':'object','list':'array'}


def is_factory(code):
    return any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == 'build_tool' for n in ast.parse(code).body)


def interface(code):
    configuration = describe(code, 'build_tool', set())
    outputs = declared_outputs(code)
    # Reuse the literal field validator for the runtime input schema.
    tree = ast.parse(code)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    if target.id == 'OUTPUT_SCHEMA': target.id = '_ignored_outputs'
                    elif target.id == 'INPUT_SCHEMA': target.id = 'OUTPUT_SCHEMA'
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == 'OUTPUT_SCHEMA': node.target.id = '_ignored_outputs'
            elif node.target.id == 'INPUT_SCHEMA': node.target.id = 'OUTPUT_SCHEMA'
    inputs = declared_outputs(ast.unparse(tree), allow_empty=True)
    if inputs is None or outputs is None:
        raise ValueError('Factory tools must declare literal INPUT_SCHEMA and OUTPUT_SCHEMA lists.')
    for field in inputs:
        if field['type'] not in JSON_TYPES:
            raise ValueError('Tool inputs need concrete types: str, int, float, bool, dict, list.')
        if not field['name'].isidentifier() or field['name'] == 'self' or keyword.iskeyword(field['name']):
            raise ValueError('Tool input names must be Python identifiers other than self.')
    return {'configuration':configuration,'inputs':inputs,'outputs':outputs}


def validate_values(fields, values, label):
    if not isinstance(values, dict): raise ValueError(f'{label} must be an object.')
    types = {'str':str,'int':int,'float':(float,int),'bool':bool,'dict':dict,'list':list}
    for field in fields:
        name = field['name']
        if name not in values:
            if field.get('required', True): raise ValueError(f'Missing {label} field: {name}')
            continue
        value = values[name]
        if value is None and field.get('nullable'): continue
        kind = field['type']
        if kind != 'any' and (not isinstance(value,types[kind]) or kind in ('int','float') and isinstance(value,bool)):
            raise ValueError(f'{label} field {name} must be {kind}.')
    json.dumps(values, allow_nan=False)


def execute(namespace, code, config, inputs):
    schema = interface(code)
    validate_values(schema['inputs'], inputs, 'Input')
    extra = set(inputs) - {f['name'] for f in schema['inputs']}
    if extra: raise ValueError(f'Unknown input fields: {sorted(extra)}')
    values = arguments(code, config, 'build_tool', set())
    signature = inspect.signature(namespace['build_tool'])
    kwargs = {k:values[k] for k in signature.parameters if k in values}
    tool = namespace['build_tool'](**kwargs)
    method = getattr(tool, 'run', None)
    if not callable(method) or inspect.iscoroutinefunction(method):
        raise ValueError('build_tool must return an object with synchronous run(inputs).')
    inspect.signature(method).bind(inputs)
    result = method(inputs)
    validate_values(schema['outputs'], result, 'Output')
    return result
