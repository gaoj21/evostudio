"""Uploaded evaluator code with a generated, typed parameter interface."""
import ast
import contextlib
import inspect
import io
import json
from backend.features.data.dataset_interface import describe, arguments

PROVIDED = {'records', 'config', 'label_records'}


def entrypoint(code):
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise ValueError(f'Python syntax error on line {exc.lineno}: {exc.msg}') from exc
    names = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    if 'build_evaluator' in names and 'evaluate' in names:
        raise ValueError('Choose one entrypoint: build_evaluator or legacy evaluate, not both.')
    return 'build_evaluator' if 'build_evaluator' in names else 'evaluate'


def interface(code):
    name = entrypoint(code)
    return describe(code, name, PROVIDED - {'records'} if name == 'build_evaluator' else PROVIDED)


def execute(payload):
    name = entrypoint(payload['code'])
    values = arguments(payload['code'], payload.get('config') or {}, name, PROVIDED - {'records'} if name == 'build_evaluator' else PROVIDED)
    namespace = {'__name__':'studio_evaluator'}
    with contextlib.redirect_stdout(io.StringIO()):
        exec(compile(payload['code'], '<evaluate.py>', 'exec'), namespace)
        function = namespace[name]
        signature = inspect.signature(function)
        kwargs = {key:values[key] for key in signature.parameters if key in values}
        if name == 'evaluate' and 'records' in signature.parameters: kwargs['records'] = payload['records']
        if 'config' in signature.parameters: kwargs['config'] = values
        if 'label_records' in signature.parameters: kwargs['label_records'] = values.get('label_records', [])
        signature.bind(**kwargs)
        result = function(**kwargs)
        if name == 'build_evaluator':
            method = getattr(result, 'evaluate', None)
            if not callable(method) or inspect.iscoroutinefunction(method):
                raise ValueError('build_evaluator must return an object with a synchronous evaluate(records) method.')
            try:
                inspect.signature(method).bind(payload['records'])
            except TypeError as exc:
                raise ValueError('Evaluator.evaluate must accept records without additional required arguments.') from exc
            result = method(payload['records'])
    json.dumps(result, allow_nan=False)
    return result
