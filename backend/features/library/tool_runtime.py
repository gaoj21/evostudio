"""How a custom toolkit runs: loading its module, building its object and
calling one of its tools.

Shared by Studio's isolated tool worker (tool_worker.py) and by exported
projects, which vendor this file as vendor/tool_runtime.py -- so it uses the
standard library only.

A toolkit is a Python module. It exposes its tools in one of three ways:

- functions: every public top-level function is a tool;
- a class: every public method of one class is a tool. The class is the one
  public class in the module, the one `TOOL_CLASS = ...` names, or whatever
  `build_tool(...)` returns. Its constructor's (or build_tool's) typed
  parameters are the toolkit's configuration, set once when it is saved, and
  one instance serves every call of a run, so it can keep state;
- the older `build_tool` + `INPUT_SCHEMA` / `run(inputs)` contract
  (python_tool.py), kept as it was.
"""
import inspect
import os
import sys
import textwrap
import types
from pathlib import Path

# The filename pasted code is compiled under; errors name lines in it.
ENTRY_FILENAME = '<tool.py>'

# Configuration field types (the DataLoader's spelling) and Python checks.
CONFIG_TYPES = {'str': str, 'int': int, 'float': (int, float), 'bool': bool, 'list': list, 'dict': dict}
# Parameter annotations the model can be told about, as JSON-schema types.
TYPE_FROM_ANNOTATION = {'str': 'string', 'int': 'integer', 'float': 'number', 'bool': 'boolean',
                        'dict': 'object', 'list': 'array'}


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

def parse_docstring(raw):
    """A docstring's summary and its per-parameter descriptions.

    Reads the Google style the standard library and most tooling use: the text
    up to an `Args:` line describes the function, and the indented lines under
    it describe one parameter each.
    """
    summary_lines, params = [], {}
    in_args, current = False, None
    for line in textwrap.dedent(raw or '').splitlines():
        stripped = line.strip()
        if stripped.lower() in ('args:', 'arguments:', 'parameters:'):
            in_args = True
            continue
        if in_args:
            if stripped and not line.startswith((' ', '\t')) and stripped.endswith(':'):
                in_args = False        # a following section: Returns:, Raises:
                continue
            if not stripped:
                continue
            name, sep, text = stripped.partition(':')
            if sep and name.strip().isidentifier():
                current = name.strip()
                params[current] = text.strip()
            elif current:
                params[current] = f'{params[current]} {stripped}'.strip()
        else:
            summary_lines.append(stripped)
    return ' '.join(line for line in summary_lines if line).strip(), params


def tool_schema(name, doc, params, is_async=False, star=False):
    """One tool's schema from its parts: {name, description, params}.

    params: (parameter name, annotation name or None, has a default). Refuses
    what the model could not be told about rather than guessing.
    """
    if is_async:
        raise ValueError(f"'{name}' is async. A tool is called directly in a "
                         "subprocess, so it has to be an ordinary function.")
    description, docs = parse_docstring(doc)
    if not description:
        raise ValueError(f"'{name}' needs a docstring: it is what the model reads to "
                         "decide when to call it. A tool without a description is not a "
                         "tool — prefix the name with an underscore if it is a helper.")
    if star:
        raise ValueError(f"'{name}' takes *args/**kwargs — a tool's parameters have to "
                         "be named, so the model knows what it can pass.")
    out = []
    for param, annotation, has_default in params:
        if param == 'self':
            raise ValueError("'self' is not a valid parameter name")
        if annotation is None:
            raise ValueError(f"Parameter '{param}' of '{name}' has no type annotation. "
                             f"Annotate it (one of: {', '.join(sorted(TYPE_FROM_ANNOTATION))}) "
                             "— the model uses the type to decide what to pass.")
        json_type = TYPE_FROM_ANNOTATION.get(annotation.lower())
        if json_type is None:
            raise ValueError(f"Parameter '{param}' of '{name}' is annotated {annotation!r}, "
                             "which has no equivalent the model can be told about. Use one "
                             f"of: {', '.join(sorted(TYPE_FROM_ANNOTATION))}.")
        field = {'name': param, 'type': json_type, 'description': docs.get(param, '')}
        if has_default:
            field['required'] = False
        out.append(field)
    return {'name': name, 'description': description, 'params': out}


def _annotation_text(annotation):
    """The plain name of a live annotation: str, list[str] -> list, 'int'."""
    if annotation is inspect.Parameter.empty:
        return None
    if isinstance(annotation, str):
        text = annotation.split('[')[0].strip()
        return text.split('.')[-1] or None
    origin = getattr(annotation, '__origin__', None)
    if isinstance(origin, type):
        return origin.__name__
    return getattr(annotation, '__name__', None)


def _config_type(annotation, default):
    """A configuration field's type and choices from a live annotation."""
    origin = getattr(annotation, '__origin__', None)
    if getattr(origin, '_name', None) == 'Literal' or str(origin).endswith('Literal'):
        choices = list(annotation.__args__)
        return _value_type(choices[0]), choices
    text = _annotation_text(annotation)
    if text in CONFIG_TYPES:
        return text, None
    return (_value_type(default) if default is not inspect.Parameter.empty else 'any'), None


def _value_type(value):
    return {str: 'str', int: 'int', float: 'float', bool: 'bool', list: 'list', dict: 'dict'}.get(type(value), 'any')


def signature_fields(callable_):
    """Configuration fields from a constructor's or factory's signature, in
    the shape the DataLoader's interface uses."""
    fields = []
    try:
        parameters = inspect.signature(callable_).parameters.values()
    except (TypeError, ValueError):       # a builtin with no readable signature
        return fields
    for param in parameters:
        if param.name in ('self', 'config') or param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        kind, choices = _config_type(param.annotation, param.default)
        field = {'name': param.name, 'type': kind, 'required': param.default is inspect.Parameter.empty,
                 'origin': 'parameter'}
        if param.default is not inspect.Parameter.empty and param.default is not None:
            field['default'] = param.default
        if choices is not None:
            field['options'] = choices
        fields.append(field)
    return fields


def method_tools(cls, owned=lambda filename: True):
    """The tools a class exposes: its public methods, in definition order.

    Only methods written in the toolkit's own code count (`owned` tells by
    filename): a base class from an installed library brings its whole API,
    none of which the author described as a tool.
    """
    found = {}
    for klass in reversed(cls.__mro__):
        if klass is object:
            continue
        for name, member in vars(klass).items():
            if name.startswith('_'):
                continue
            func = member.__func__ if isinstance(member, (staticmethod, classmethod)) else member
            if not inspect.isfunction(func) or not owned(func.__code__.co_filename):
                found.pop(name, None)
                continue
            found[name] = (member, func)
    tools = []
    for name, (member, func) in found.items():
        signature = inspect.signature(func)
        params = list(signature.parameters.values())
        if not isinstance(member, staticmethod) and params:
            params = params[1:]          # self / cls
        star = any(p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD) for p in params)
        tools.append(tool_schema(
            name, inspect.getdoc(func) or '',
            [(p.name, _annotation_text(p.annotation), p.default is not inspect.Parameter.empty)
             for p in params if p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)],
            is_async=inspect.iscoroutinefunction(func), star=star))
    return tools


# ---------------------------------------------------------------------------
# Loading and building
# ---------------------------------------------------------------------------

def load_entry(code, package_dir=None, entry_file=None, module_name='studio_tool'):
    """Execute a toolkit's entry module; returns (namespace, filename).

    Pasted code runs as a module of its own. An uploaded folder goes first on
    sys.path, so its modules import each other, and an entry inside a package
    (`pkg/__init__.py`, `pkg/api.py`) runs under its dotted name, so its
    relative imports work. The code run is `code` -- the saved entry, or a
    draft of it -- not necessarily the file on disk.
    """
    import importlib
    import importlib.util
    if not package_dir:
        module = types.ModuleType(module_name)
        module.__file__ = ENTRY_FILENAME
        sys.modules[module_name] = module
        exec(compile(code, ENTRY_FILENAME, 'exec'), module.__dict__)
        return module.__dict__, ENTRY_FILENAME
    folder = Path(package_dir).resolve()
    entry = entry_file or 'tools.py'
    path = folder / entry
    for directory in (str(folder), str(path.parent)):
        if directory not in sys.path:
            sys.path.insert(0 if directory == str(folder) else 1, directory)
    parts = list(Path(entry).with_suffix('').parts)
    is_package = parts[-1] == '__init__'
    if is_package:
        parts = parts[:-1]
    if len(parts) > 1 or is_package:
        name = '.'.join(parts)
        for i in range(1, len(parts)):
            importlib.import_module('.'.join(parts[:i]))
    else:
        # A top-level file has no package to be relative to; a private name
        # keeps it from shadowing anything it shares a name with.
        name = module_name
    spec = importlib.util.spec_from_file_location(
        name, str(path), submodule_search_locations=[str(path.parent)] if is_package else None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    if '.' in name:
        parent, _, last = name.rpartition('.')
        setattr(sys.modules[parent], last, module)
    try:
        exec(compile(code, str(path), 'exec'), module.__dict__)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module.__dict__, str(path)


def apply_config(fields, values):
    """Configuration values checked against their fields, defaults filled in."""
    result = dict(values or {})
    for field in fields or []:
        key = field['name']
        if key not in result:
            if field.get('required'):
                raise ValueError(f'Missing configuration: {key}')
            if field.get('default') is not None:
                result[key] = field['default']
            continue
        value = result[key]
        if value is None and not field.get('required'):
            continue
        expected = CONFIG_TYPES.get(field.get('type'))
        if expected and (not isinstance(value, expected) or field['type'] in ('int', 'float') and isinstance(value, bool)):
            raise ValueError(f'Configuration {key} must be {field["type"]}.')
        if 'options' in field and value not in field['options']:
            raise ValueError(f'Configuration {key} must be one of {field["options"]}.')
    return result


def maker(namespace, target):
    """The class or factory a class toolkit is built with."""
    key = (target or {}).get('factory') or (target or {}).get('class')
    found = namespace.get(key) if key else None
    if not callable(found):
        raise ValueError(f'The toolkit does not define {key!r}; it is what builds the tool object.')
    return found


def build_instance(namespace, target, fields, config):
    """One instance of a class toolkit, built from its saved configuration.

    Typed parameters are passed by name; a parameter called `config` gets the
    whole configuration, as a DataLoader's build_dataset does.
    """
    build = maker(namespace, target)
    values = apply_config(fields, config)
    signature = inspect.signature(build)
    kwargs = {key: values[key] for key in signature.parameters if key in values and key != 'config'}
    if 'config' in signature.parameters:
        kwargs['config'] = values
    signature.bind(**kwargs)
    return build(**kwargs)


def call_method(instance, name, args):
    """Call one tool of a class toolkit's instance."""
    method = getattr(instance, name, None) if not name.startswith('_') else None
    if not callable(method):
        raise ValueError(f'The tool object has no public method {name!r}.')
    return method(**(args or {}))


def discover(namespace, target, fields, config, owned=lambda filename: True):
    """What a class toolkit exposes, read off the live objects.

    For a class the pasted code imports (TOOL_CLASS = SomeImportedClass) or a
    build_tool that does not say what it returns: the class is read from the
    module, and only for an unannotated build_tool is the object built.
    """
    build = maker(namespace, target)
    if inspect.isclass(build):
        cls, configuration = build, signature_fields(build)
    else:
        configuration = signature_fields(build)
        returned = inspect.signature(build).return_annotation
        if isinstance(returned, str):
            returned = namespace.get(returned, returned)
        cls = returned if inspect.isclass(returned) else type(build_instance(namespace, target, configuration, config))
    tools = method_tools(cls, owned)
    if not tools:
        raise ValueError(f'{cls.__name__} has no public methods to expose as tools.')
    doc = inspect.getdoc(cls) or ''
    return {'class': cls.__name__, 'class_description': parse_docstring(doc)[0],
            'configuration': configuration, 'tools': tools}


def owned_by(filename, package_dir=None):
    """Whether code in a file is the toolkit's own (not an installed library)."""
    root = os.path.join(str(Path(package_dir).resolve()), '') if package_dir else None

    def owned(name):
        return name == filename or (root is not None and os.path.abspath(name).startswith(root))
    return owned
