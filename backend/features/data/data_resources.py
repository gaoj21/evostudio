"""Immutable uploaded files/directories; parsing belongs to DataLoaders."""
import hashlib
import json
import re
import shutil
import uuid
import threading
from pathlib import PurePosixPath
from fastapi import APIRouter, HTTPException, Request, Body
from backend.api.studio_config import data_path
from backend.api.sources import SourceError

_association_lock = threading.Lock()

router = APIRouter(prefix='/api/data-resources', tags=['DataLoader'])


def directory():
    root = data_path('data-resources')
    root.mkdir(parents=True, exist_ok=True)
    return root


def path_for(resource_id):
    if not isinstance(resource_id, str) or not re.fullmatch('[a-f0-9]{32}', resource_id):
        raise SourceError('Invalid data resource ID.')
    return directory() / resource_id


def load(resource_id):
    path = path_for(resource_id) / 'manifest.json'
    if not path.is_file():
        raise SourceError('Data resource not found. Select or upload a resource.')
    return json.loads(path.read_text())


def safe_name(name):
    normalized = (name or '').replace('\\', '/')
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or any(part in ('', '.', '..') for part in normalized.split('/')) or ':' in normalized or '\x00' in normalized:
        raise SourceError('File names must be relative paths without parent traversal.')
    if normalized == 'manifest.json' or normalized.startswith('.upload-'):
        # Files live in their own directory, but keep generated names unambiguous.
        return normalized
    return path.as_posix()


def public(item):
    return {**item, 'root': str((path_for(item['id']) / 'files').resolve())}


@router.get('')
def listing():
    return {'resources': [public(json.loads(p.read_text())) for p in sorted(directory().glob('*/manifest.json')) if not p.parent.name.startswith('.')]}



@router.post('')
async def upload(request: Request):
    form = await request.form(max_files=10000, max_fields=10000)
    files = form.getlist('files') or form.getlist('file')
    if not files or any(not hasattr(file, 'read') for file in files):
        raise HTTPException(422, 'Choose files or a folder.')
    resource_id = uuid.uuid4().hex
    temporary = directory() / ('.upload-' + resource_id)
    temporary.mkdir()
    entries = []
    names = set()
    try:
        from .user_datasets import MAX_BYTES
        total = 0
        for file in files:
            name = safe_name(file.filename)
            if name in names:
                raise SourceError(f'Duplicate file path: {name}')
            names.add(name)
            path = temporary / 'files' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            size = 0
            with path.open('wb') as output:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    total += len(chunk)
                    if MAX_BYTES and total > MAX_BYTES:
                        raise SourceError('Resource exceeds the configured upload limit.')
                    digest.update(chunk)
                    output.write(chunk)
            entries.append({'path': name, 'bytes': size, 'sha256': digest.hexdigest()})
        entries.sort(key=lambda item: item['path'])
        item = {'id': resource_id, 'name': str(form.get('name') or files[0].filename.split('/')[0])[:120],
                'files': entries, 'size_bytes': total, 'graph_id': str(form.get('graph_id') or ''),
                'version': hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()}
        (temporary / 'manifest.json').write_text(json.dumps(item))
        temporary.rename(path_for(resource_id))
        return public(item)
    except SourceError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
        for file in files:
            await file.close()


@router.delete('/{resource_id}')
def remove(resource_id: str):
    from backend.api import graphs
    try:
        load(resource_id)
        used = []
        for path in graphs.GRAPHS_DIR.glob('*.json'):
            graph = json.loads(path.read_text())
            if any((t.get('source') or {}).get('resource_id') == resource_id or ((t.get('evaluator') or {}).get('labels') or {}).get('resource_id') == resource_id for t in graph.get('tasks', [])):
                used.append(graph.get('name', graph['id']))
        if used:
            raise HTTPException(409, 'Resource is used by saved workflows: ' + ', '.join(used))
        shutil.rmtree(path_for(resource_id))
        return {'deleted': resource_id}
    except SourceError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post('/{resource_id}/workspace')
def attach_workspace(resource_id: str, body: dict = Body(...)):
    """Mount a previously uploaded dataset without requiring a canvas save."""
    from backend.api import graphs
    graph_id = body.get('graph_id')
    if not isinstance(graph_id, str) or not graphs.load_graph(graph_id):
        raise HTTPException(404, 'Workflow not found.')
    try:
        with _association_lock:
            item = load(resource_id)
            graph_ids = set(item.get('workspace_graph_ids') or [])
            graph_ids.add(graph_id)
            item['workspace_graph_ids'] = sorted(graph_ids)
            temporary = path_for(resource_id) / ('.manifest-' + uuid.uuid4().hex)
            temporary.write_text(json.dumps(item))
            temporary.replace(path_for(resource_id) / 'manifest.json')
        return public(item)
    except SourceError as exc:
        raise HTTPException(404, str(exc)) from exc
