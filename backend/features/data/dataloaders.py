"""DataLoader contract: resource + configuration -> validated, ordered records.

Legacy sources are adapters. Raw resources stay immutable. Transform functions
run deterministically through the existing tool registry, not an LLM planner.
"""
import copy
import fnmatch
import hashlib
import json
import threading
import uuid
from backend.api.studio_config import data_path

# One lock per prepared-data cache key, so preparing one dataset never makes
# another workflow's preparation wait. The guard only protects the dict.
_cache_locks: dict = {}
_cache_locks_guard = threading.Lock()


def _cache_lock_for(key):
    with _cache_locks_guard:
        return _cache_locks.setdefault(key, threading.Lock())


DEFAULT_BATCH_TIMEOUT = 120
# A full (non-streaming) read has no per-batch progress to watch, so it is
# allowed this many batch periods before it is stopped.
FULL_READ_BATCHES = 10


def batch_timeout(config):
    """Seconds a Dataset may take to produce one batch."""
    return int((config or {}).get('batch_timeout') or DEFAULT_BATCH_TIMEOUT)


def preview_timeout(config):
    value = config.get('preview_timeout', 120)
    if isinstance(value, bool) or not isinstance(value, int) or not 10 <= value <= 3600:
        raise SourceError('Sample time limit must be an integer between 10 and 3600 seconds.')
    return value


def api_backed(config):
    """A DataLoader whose reader is a remote API source: it must be collected
    first, outside any HTTP request, and never fetched as a side effect."""
    from .source_apis import is_api_source
    return (config or {}).get('loader') == 'source' and is_api_source((config or {}).get('source_config'))
from fastapi import APIRouter, Body, HTTPException
from backend.api.sources import SourceError
from . import data_resources

router = APIRouter(prefix='/api/dataloaders', tags=['DataLoader'])
LEGACY_LOADERS = [
    {'id': 'auto', 'label': 'Structured records (CSV / TSV / JSON / JSONL)'},
    {'id': 'files', 'label': 'One file per record (text or binary metadata)'},
    {'id': 'folders', 'label': 'One parent folder per record'},
    {'id': 'python', 'label': 'Python · PyTorch Dataset'},
    {'id': 'tool', 'label': 'Custom reader tool'},
    {'id': 'source', 'label': 'Existing dataset or API source'},
]
LOADERS = [{'id': 'python', 'label': 'PyTorch Dataset'}]


def check_cancel():
    from backend.api import chat_control
    control = chat_control.current.get()
    if control:
        control.check()


def get_path(value, path):
    for part in (path or '').split('.'):
        if not part:
            continue
        if isinstance(value, list) and part.isdigit():
            value = value[int(part)]
        else:
            value = value[part]
    return value


def validate(config):
    from backend.features.execution.batch_settings import loader_batch_size
    try:
        loader_batch_size({'config': {**config, 'type': 'dataloader'}})
    except ValueError as exc:
        raise SourceError(str(exc)) from exc
    if any(config.get(k) in ('read_dataset','evaluate_records') for k in ('reader_tool','transform_tool')):
        raise SourceError('Reader/transform tools must not recursively invoke the component dispatcher.')
    if config.get('loader', 'auto') not in {item['id'] for item in LEGACY_LOADERS}:
        raise SourceError('Unknown DataLoader.')
    for key, default, minimum in [('read_batch_size', 100, 1), ('offset', 0, 0), ('n', 0, 0)]:
        value = config.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise SourceError(f'{key} must be an integer >= {minimum}.')
    timeout = config.get('batch_timeout', DEFAULT_BATCH_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 10 <= timeout <= 3600:
        raise SourceError('Time allowed per batch must be a whole number of seconds between 10 and 3600.')
    if config.get('loader') == 'source':
        if not isinstance(config.get('source_config'), dict) or config['source_config'].get('type') == 'dataloader':
            raise SourceError('Choose a non-recursive source adapter.')
    elif not config.get('resource_id'):
        raise SourceError('Choose a data resource.')
    if config.get('loader') == 'python':
        import ast
        code = config.get('code')
        if not isinstance(code, str) or not code.strip():
            raise SourceError('Paste Python code or upload a .py file defining build_dataset(resource, config).')
        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            raise SourceError(f'Python syntax error on line {exc.lineno}: {exc.msg}') from exc
        if not any(isinstance(node, ast.FunctionDef) and node.name == 'build_dataset' for node in tree.body):
            raise SourceError('Define build_dataset(resource, config) returning a PyTorch Dataset.')
    if config.get('loader') == 'tool' and not config.get('reader_tool'):
        raise SourceError('Choose a custom reader tool.')
    if config.get('transform_scope', 'record') not in ('record', 'all'):
        raise SourceError('Transform scope must be record or all.')
    if config.get('input_mode', 'records') not in ('records', 'reference'):
        raise SourceError('Input mode must be records or reference.')
    if config.get('group_by') and not config.get('order_by'):
        raise SourceError('Ordered trajectories require both group and order fields.')


def call_tool(name, arguments):
    from backend.api import tools_registry
    try:
        check_cancel()
        return tools_registry.call_tool(name, arguments)
    except SourceError:
        raise
    except Exception as exc:
        raise SourceError(f"DataLoader tool '{name}' failed: {exc}") from exc


def _object_rows(rows):
    if not isinstance(rows, list) or any(not isinstance(row, dict) or not row or any(not isinstance(k, str) or not k for k in row) for row in rows):
        raise SourceError('DataLoader and transforms must return a list of non-empty record objects.')
    try:
        json.dumps(rows, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise SourceError('DataLoader output must contain finite, JSON-serializable values.') from exc
    if any('_dataloader' in row for row in rows):
        raise SourceError('_dataloader is reserved for record provenance.')
    return rows


def raw_records(config, sample_limit=None):
    loader = config.get('loader', 'auto')
    if loader == 'source':
        from .sources import records_from_source_node
        from .source_apis import collect_records, is_api_source
        source_config = config['source_config']
        # An API source is collected over its whole range (every GDELT
        # window), not executed once: a single execution is one record.
        rows = (collect_records(source_config) if is_api_source(source_config)
                else records_from_source_node({'source': source_config}))
        return rows, {'source_config': copy.deepcopy(source_config)}
    resource = data_resources.load(config.get('resource_id'))
    entries = [entry for entry in resource['files'] if fnmatch.fnmatch(entry['path'], config.get('file_pattern') or '*')]
    root = data_resources.path_for(resource['id']) / 'files'
    if loader == 'python':
        from backend.features.chat import chat_control
        try:
            rows = chat_control.worker('dataset', {'code':config['code'],
                'resource':{**resource, 'root':str(root), 'files':entries},
                'config':{**(config.get('reader_config') or {}), 'reference_inputs':config.get('reference_inputs') or {}},
                'batch_size':config.get('read_batch_size', 100), 'sample_limit':sample_limit, 'offset':config.get('offset',0), 'record_limit':config.get('n',0)},
                timeout=preview_timeout(config) if sample_limit is not None else batch_timeout(config) * FULL_READ_BATCHES)
        except Exception as exc:
            # The worker already told errors from the Dataset code in terms of
            # that code ("Dataset code line N: ..."); anything else is ours.
            message = str(exc)
            raise SourceError(message if message.startswith('Dataset code') else f'Python DataLoader failed: {message}') from exc
        return rows, resource
    if loader == 'tool':
        from backend.api import tools_registry
        rows = call_tool(config['reader_tool'], {'resource': {**resource, 'root': str(root), 'files': entries}, 'config': config.get('reader_config') or {}})
        return rows, resource
    records = []
    for entry in entries:
        check_cancel()
        path = root / data_resources.safe_name(entry['path'])
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != entry['sha256']:
            raise SourceError(f'Resource file changed: {entry["path"]}. Upload it as a new version.')
        if loader == 'auto':
            try:
                if config.get('record_path'):
                    value = get_path(json.loads(content.decode('utf-8-sig')), config['record_path'])
                    found = value if isinstance(value, list) else [value]
                else:
                    from .user_datasets import parse
                    found, _ = parse(path.name, content)
                records.extend(_object_rows(found))
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise SourceError(f'{entry["path"]}: cannot read records at the configured path ({exc}).') from exc
        else:
            try:
                text = content.decode('utf-8-sig')
                if '\x00' in text:
                    text = None
            except UnicodeDecodeError:
                text = None
            records.append({'file': entry['path'], 'content': text, 'size_bytes': entry['bytes'], 'sha256': entry['sha256']})
    if loader == 'folders':
        groups = {}
        for row in records:
            parent = row['file'].rsplit('/', 1)[0] if '/' in row['file'] else '.'
            groups.setdefault(parent, []).append(row)
        records = [{'folder': name, 'files': files} for name, files in groups.items()]
    return records, resource


def content_identity(resource):
    """The resource fields that define its bytes. Bookkeeping such as which
    workflows it is attached to, or its display name, must not change the
    snapshot or the prepared-data cache key."""
    if 'files' not in resource:
        return resource
    return {key: resource.get(key) for key in ('id', 'version', 'files')}


def _prepare(config):
    config = copy.deepcopy(config)
    config.pop('preview_snapshot', None)
    validate(config)
    check_cancel()
    rows, resource = raw_records(config)
    rows = _object_rows(rows)
    original = len(rows)
    references = config.get('reference_inputs') or {}
    if references:
        # Transforms see the shared references; they are re-attached below
        # to whatever the transform and field mapping produce.
        from .input_composition import merge
        rows = merge(rows, references)
    if config.get('transform_tool'):
        from backend.api import tools_registry
        if config.get('transform_scope', 'record') == 'all':
            rows = _object_rows(call_tool(config['transform_tool'], {'records': rows}))
        else:
            transformed = []
            for row in rows:
                check_cancel()
                result = call_tool(config['transform_tool'], {'record': row})
                if result is not None:
                    transformed.extend(_object_rows(result if isinstance(result, list) else [result]))
            rows = transformed
    mapping = config.get('field_mapping')
    if mapping:
        if not isinstance(mapping, dict) or any(not isinstance(v, str) or not v for v in mapping.values()) or len(set(mapping.values())) != len(mapping):
            raise SourceError('Field mapping needs unique non-empty output names.')
        try:
            rows = [{target: get_path(row, source) for source, target in mapping.items()} for row in rows]
        except (KeyError, IndexError, TypeError) as exc:
            raise SourceError(f'Missing mapped field: {exc}') from exc
        if any(target in references and source != target for source, target in mapping.items()):
            raise SourceError('Mapped field names overlap shared reference output names. Rename the mapped field or the reference output.')
    if references:
        # Shared references reach every record even when the transform or
        # field mapping rebuilt it from selected fields only.
        rows = [{**row, **{k: copy.deepcopy(v) for k, v in references.items() if k not in row}} for row in rows]
    group, order = config.get('group_by'), config.get('order_by')
    if group:
        try:
            rows.sort(key=lambda row: (str(get_path(row, group)), get_path(row, order)))
        except (KeyError, TypeError) as exc:
            raise SourceError('Every trajectory record must have comparable group/order fields.') from exc
    rows = _object_rows(rows)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    rows = [{key: row.get(key) for key in fields} for row in rows]
    digest = hashlib.sha256(json.dumps({'config': config, 'resource': content_identity(resource), 'records': rows}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    offset, limit = config.get('offset', 0), config.get('n', 0)
    if config.get('loader') != 'python':
        rows = rows[offset:offset + limit if limit else None]
    for i, row in enumerate(rows, offset):
        row['_dataloader'] = {'snapshot': digest, 'record_id': f'{digest[:16]}:{i}',
                              'resource_id': resource.get('id'), 'resource_version': resource.get('version'),
                              'group': get_path(row, group) if group else None,
                              'order': get_path(row, order) if group else None}
    if config.get('input_mode') == 'reference':
        field = config.get('reference_field') or 'reference_data'
        if not isinstance(field, str) or field == '_dataloader':
            raise SourceError('Invalid shared reference field.')
        rows = [{field: rows}]
    return rows, {'snapshot': digest, 'config': copy.deepcopy(config), 'raw_records': original,
                  'output_records': len(rows), 'resource_version': resource.get('version')}


def _prepare_cached(config):
    config = copy.deepcopy(config)
    config.pop('input_schema', None)
    config.pop('output_schema', None)
    config.pop('preview_snapshot', None)
    validate(config)
    # Live source adapters always reload. Immutable file resources can reuse
    # preparation across preview, Run and Evolve, including custom transforms.
    if config.get('loader') == 'source' or config.get('cache', True) is False or (config.get('reader_config') or {}).get('path'):
        return _prepare(config)
    resource = data_resources.load(config.get('resource_id'))
    for entry in resource['files']:
        path = data_resources.path_for(resource['id']) / 'files' / data_resources.safe_name(entry['path'])
        digest_state = hashlib.sha256()
        with path.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                check_cancel()
                digest_state.update(chunk)
        digest = digest_state.hexdigest()
        if digest != entry['sha256']:
            raise SourceError(f"Resource file changed: {entry['path']}. Upload a new version.")
    from backend.api import custom_tools
    tool_versions = {config[key]: custom_tools.find(config[key]) for key in ('reader_tool', 'transform_tool') if config.get(key)}
    key = hashlib.sha256(json.dumps({'config':config, 'resource':content_identity(resource), 'tools':tool_versions, 'contract':4},sort_keys=True,default=str).encode()).hexdigest()
    root = data_path('dataloader-cache')
    root.mkdir(parents=True, exist_ok=True)
    path = root / (key + '.json')
    with _cache_lock_for(key):
        check_cancel()
        if path.exists():
            rows, meta = json.loads(path.read_text())
            return rows, meta
        rows, meta = _prepare(config)
        temporary = root / (key + '.' + uuid.uuid4().hex + '.tmp')
        temporary.write_text(json.dumps([rows,meta],ensure_ascii=False,allow_nan=False))
        temporary.replace(path)
        return rows, meta


def prepare(config):
    """Return materialized records without loading native runtimes in the API."""
    rows, meta = _prepare_cached(config)
    return rows, {**meta, 'engine': 'torch.utils.data.DataLoader'}


def records(config):
    return [row for chunk in iter_batches(config) for row in chunk]


def iter_batches(config):
    """Prepared records in consecutive batches of read_batch_size.

    The same batches torch's DataLoader makes for a list with shuffle off,
    no dropped tail and the identity collator, cut here instead of in a
    worker: shipping every record to a subprocess only to slice it cost a
    full copy and could time out on large datasets.
    """
    rows, _ = _prepare_cached(config)
    size = config.get('read_batch_size', 100)
    for start in range(0, len(rows), size):
        check_cancel()
        yield rows[start:start + size]


@router.get('')
def catalog():
    return {'loaders': LOADERS}


@router.post('/interface')
def code_interface(body: dict = Body(...)):
    from .dataset_interface import describe
    try:
        return describe(body.get('code') or '')
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc


def _sample(rows, field):
    value = next((row[field] for row in rows if row.get(field) is not None), None)
    encoded = json.dumps(value, ensure_ascii=False)
    return value if len(encoded) <= 1000 else encoded[:1000] + '…'


@router.post('/preview')
def preview(config: dict = Body(...)):
    try:
        config = copy.deepcopy(config)
        graph, name = config.pop('_graph', None), config.pop('_node', None)
        sample_requested = config.pop('preview_mode', 'interface') == 'sample'
        if config.get('loader') == 'python':
            from .dataset_interface import declared_outputs, describe
            # Reading a code contract must not depend on resources or Run settings.
            code = config.get('code') or ''
            describe(code)
            fields = None if sample_requested else declared_outputs(code)
            if fields is not None and not sample_requested:
                if config.get('input_mode') == 'reference':
                    field = config.get('reference_field') or 'reference_data'
                    if not isinstance(field, str) or not field.strip() or field == '_dataloader':
                        raise SourceError('Use a non-empty reference output name other than _dataloader.')
                    fields = [{'name':field,'type':'list','required':True,'nullable':False}]
                return {'fields':fields,'preview':[],'preview_mode':'declared','sample_count':0,'snapshot':None}
            if not sample_requested:
                raise SourceError('Add a top-level OUTPUT_SCHEMA list to identify output fields without loading data. Alternatively, explicitly choose Sample 1 record to execute your Dataset with its configured sample time limit.')
            validate(config)
            if graph:
                from .input_composition import references
                if references(graph, {'name': name}):
                    raise SourceError('Declare OUTPUT_SCHEMA to inspect this Input without loading its shared reference datasets. Full references are supplied during Run.')
            # Interface inspection must never prepare/cache/hash the full dataset.
            rows, _ = raw_records(config, sample_limit=1)
            rows = _object_rows(rows)
            sample_count = len(rows)
            if config.get('input_mode') == 'reference':
                rows = [{config.get('reference_field') or 'reference_data':rows}]
            meta = {'engine':'torch.utils.data.DataLoader','preview_mode':'sample','sample_count':sample_count,'sample_limit':1,'snapshot':None}
            return _preview_response(rows, meta)
        raise SourceError('This legacy reader cannot declare a static interface. Replace it with a Python Dataset and OUTPUT_SCHEMA before inspecting outputs.')
    except (SourceError, ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc


def _preview_response(rows, meta):
    fields = list(dict.fromkeys(k for row in rows for k in row if k != '_dataloader'))
    from .user_datasets import field_types
    types = field_types({'fields': fields, 'records': rows})
    return {**meta, 'fields': [{'name': f, 'type': types[f], 'required': all(f in row and row[f] is not None for row in rows), 'nullable': any(row.get(f) is None for row in rows), 'sample': _sample(rows, f)} for f in fields],
            'preview': [{k: (str(v)[:1000] + '…' if len(str(v)) > 1000 else v) for k, v in row.items()} for row in rows[:5]]}
