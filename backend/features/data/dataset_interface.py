"""Extract editable Dataset arguments without importing/executing user code."""
import ast
import hashlib


RESERVED = {'resource', 'config', 'reference_inputs'}


def _type(value):
    return {str:'str',int:'int',float:'float',bool:'bool',list:'list',dict:'dict',type(None):'any'}.get(type(value),'any')


def _annotation(node):
    text = ast.unparse(node) if node is not None else ''
    if isinstance(node, ast.Subscript) and ast.unparse(node.value).split('.')[-1] == 'Literal':
        values = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
        choices = [ast.literal_eval(v) for v in values]
        return _type(choices[0]), choices
    if text in ('str','int','float','bool','list','dict'): return text, None
    if text.startswith(('list[','List[')): return 'list', None
    if text.startswith(('dict[','Dict[')): return 'dict', None
    if 'Optional[' in text or '| None' in text:
        base = text.replace('typing.','').replace('Optional[','').rstrip(']').replace(' | None','')
        if base in ('str','int','float','bool','list','dict'): return base, None
    return 'any', None


def describe(code, entrypoint="build_dataset", provided=None):
    reserved = set(provided) if provided is not None else RESERVED
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise ValueError(f'Python syntax error on line {exc.lineno}: {exc.msg}') from exc
    definitions = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == entrypoint]
    if len(definitions) > 1: raise ValueError(f'Define {entrypoint} exactly once.')
    factory = definitions[0] if definitions else None
    if factory is not None and not isinstance(factory, ast.FunctionDef):
        raise ValueError(f'{entrypoint} must be a synchronous factory function, not a class or async function.')
    if factory is not None and factory.decorator_list:
        raise ValueError(f'{entrypoint} must not use decorators; decorate internal helpers instead.')
    if factory is None: raise ValueError(f'Define {entrypoint} with named, typed input parameters.')
    if factory.args.posonlyargs or factory.args.vararg or factory.args.kwarg:
        raise ValueError(f'Use named parameters for {entrypoint}; positional-only parameters and *args/**kwargs cannot generate a form.')
    params, warnings = {}, []
    args = factory.args.args
    defaults = [None] * (len(args)-len(factory.args.defaults)) + list(factory.args.defaults)
    for arg, default in [*zip(args,defaults), *zip(factory.args.kwonlyargs,factory.args.kw_defaults)]:
        if arg.arg in reserved: continue
        kind, choices = _annotation(arg.annotation)
        field = {'name':arg.arg,'type':kind,'required':default is None,'origin':'parameter'}
        if default is not None:
            try: value = ast.literal_eval(default)
            except (ValueError, TypeError): raise ValueError(f'Use a literal default for parameter {arg.arg}.')
            field['default'] = value
            if kind=='any': field['type'] = _type(value)
        if choices is not None: field['options'] = choices
        params[arg.arg] = field
    # Backward-compatible discovery for config.get("x", default) / config["x"].
    has_config = any(a.arg=='config' for a in [*factory.args.args,*factory.args.kwonlyargs])
    for node in (ast.walk(tree) if has_config else []):
        key, default, required = None, None, False
        if isinstance(node,ast.Subscript) and isinstance(node.value,ast.Name) and node.value.id=='config':
            if isinstance(node.slice,ast.Constant) and isinstance(node.slice.value,str): key=node.slice.value
            else: warnings.append('Dynamic config keys cannot be inferred. Declare named, typed factory parameters.')
            required = isinstance(node.ctx, ast.Load)
        elif isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and isinstance(node.func.value,ast.Name) and node.func.value.id=='config' and node.func.attr=='get':
            if node.args and isinstance(node.args[0],ast.Constant) and isinstance(node.args[0].value,str): key=node.args[0].value
            else: warnings.append('Dynamic config keys cannot be inferred. Declare named, typed factory parameters.')
            if len(node.args)>1:
                try: default=ast.literal_eval(node.args[1])
                except (ValueError,TypeError): warnings.append(f'Cannot infer the default for config.{key}; use a literal default.')
        if key and key not in reserved:
            field={'name':key,'type':_type(default),'default':default,'required':required,'origin':'config'}
            if key not in params: params[key]=field
            elif params[key]['origin']=='config':
                params[key]['required'] |= required
                if params[key]['type']=='any' and field['type']!='any': params[key].update(type=field['type'],default=default)
    output_info, needs = {}, {}
    if entrypoint == 'build_dataset':
        needs = {'dependencies': dependencies(tree)}
        try:
            fields = declared_outputs(code)
            output_info = {'outputs': fields or [], 'output_schema_available': fields is not None,
                           'output_status': 'Declared OUTPUT_SCHEMA; no code was executed.' if fields is not None else
                           'No OUTPUT_SCHEMA declared. Sample 5 records to detect and apply outputs.'}
        except (ValueError, TypeError) as exc:
            output_info = {'output_schema_available': False, 'output_schema_error': str(exc),
                           'output_status': 'Fix OUTPUT_SCHEMA or sample records to detect outputs.'}
    return {'entrypoint':entrypoint, 'code_hash':hashlib.sha256(code.encode()).hexdigest(),'inputs':list(params.values()),
            'provided_inputs':sorted(reserved - {'config'}), 'warnings':sorted(set(warnings)),
            'outputs':[], 'output_status':'Run a preview with your inputs to infer output fields.', **output_info, **needs}


def arguments(code, values, entrypoint="build_dataset", provided=None):
    result = dict(values)
    for field in describe(code, entrypoint, provided)['inputs']:
        key=field['name']
        if key not in result:
            if field['required']: raise ValueError(f'Missing input: {key}')
            if field.get('default') is not None: result[key]=field['default']
            continue
        value=result[key]
        if value is None and not field['required']: continue
        types={'str':str,'int':int,'float':(int,float),'bool':bool,'list':list,'dict':dict}
        expected=types.get(field['type'])
        if expected and (not isinstance(value,expected) or field['type'] in ('int','float') and isinstance(value,bool)):
            raise ValueError(f'Input {key} must be {field["type"]}.')
        if 'options' in field and value not in field['options']: raise ValueError(f'Input {key} must be one of {field["options"]}.')
    return result


def declared_outputs(code, allow_empty=False):
    """Read an optional literal schema without importing or running user code."""
    tree = ast.parse(code)
    declarations = [n.value for n in tree.body if
                    (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'OUTPUT_SCHEMA' for t in n.targets)) or
                    (isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == 'OUTPUT_SCHEMA')]
    if not declarations:
        return None
    if len(declarations) != 1:
        raise ValueError('Define OUTPUT_SCHEMA exactly once.')
    try:
        fields = ast.literal_eval(declarations[0])
    except (ValueError, TypeError) as exc:
        raise ValueError('OUTPUT_SCHEMA must be a literal list of field definitions.') from exc
    if not isinstance(fields, list) or (not fields and not allow_empty):
        raise ValueError('OUTPUT_SCHEMA must be a non-empty list.')
    names, result = set(), []
    for field in fields:
        if not isinstance(field, dict) or not isinstance(field.get('name'), str) or not field['name'].strip():
            raise ValueError('Each OUTPUT_SCHEMA field needs a name and type.')
        if field['name'] in names or field['name'] == '_dataloader':
            raise ValueError('OUTPUT_SCHEMA field names must be unique and not reserved.')
        if field.get('type') not in ('str','int','float','bool','list','dict','any'):
            raise ValueError('OUTPUT_SCHEMA types: str, int, float, bool, list, dict, any.')
        for key in ('required', 'nullable'):
            if key in field and type(field[key]) is not bool:
                raise ValueError(f'OUTPUT_SCHEMA {key} must be boolean.')
        names.add(field['name'])
        result.append({'name':field['name'], 'type':field['type'], 'required':field.get('required',True), 'nullable':field.get('nullable',False)})
    return result


def dependencies(tree):
    """The packages (and spaCy models) the code imports or loads by name, and
    whether Studio's worker Python has them. Found with the import system's
    finder only: nothing is imported and no user code runs."""
    import importlib.util
    import sys
    from backend.features.user_code import install_command, spacy_model_command
    # `try: import x / except ImportError:` is an optional dependency.
    optional = {id(inner) for node in ast.walk(tree) if isinstance(node, ast.Try)
                for statement in node.body for inner in ast.walk(statement)}
    names, models = [], []
    for node in ast.walk(tree):
        if id(node) in optional:
            continue
        if isinstance(node, ast.Import):
            names += [alias.name.split('.')[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module.split('.')[0])
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'load'
              and isinstance(node.func.value, ast.Name) and node.func.value.id == 'spacy'
              and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
              and node.args[0].value.isidentifier()):
            models.append(node.args[0].value)

    from backend.features.user_code import in_studio_checkout

    def available(name):
        """Installed, and not merely one of Studio's own folders (which a
        worker puts last on its path: the import would not find it)."""
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            return False
        if spec is None:
            return False
        if name in ('backend', 'evoagentx'):
            return True
        places = [*(spec.submodule_search_locations or []), *([spec.origin] if spec.origin and spec.has_location else [])]
        return not places or not all(in_studio_checkout(place) for place in places)
    result = [{'name': name, 'kind': 'package', 'available': available(name), 'install': install_command(name)}
              for name in dict.fromkeys(names) if name not in sys.stdlib_module_names]
    result += [{'name': name, 'kind': 'spacy model', 'available': available(name), 'install': spacy_model_command(name)}
               for name in dict.fromkeys(models)]
    return result
