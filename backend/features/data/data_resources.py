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
from backend.features.persistence import write_json

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
    return json.loads(path.read_text(encoding='utf-8'))


def safe_name(name):
    normalized = (name or '').replace('\\', '/')
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or any(part in ('', '.', '..') for part in normalized.split('/')) or ':' in normalized or '\x00' in normalized:
        raise SourceError('File names must be relative paths without parent traversal.')
    if normalized == 'manifest.json' or normalized.startswith('.upload-'):
        # Files live in their own directory, but keep generated names unambiguous.
        return normalized
    return path.as_posix()


def create_from_records(records, name, graph_id='', origin=None):
    """Store collected records as an immutable resource: one records.jsonl.

    The same kind of resource an upload makes, so any DataLoader (a PyTorch
    Dataset included) can read it, and every later run reads the same bytes
    rather than fetching again.
    """
    resource_id = uuid.uuid4().hex
    temporary = directory() / ('.upload-' + resource_id)
    (temporary / 'files').mkdir(parents=True)
    try:
        body = ''.join(json.dumps(r, ensure_ascii=False, allow_nan=False) + '\n' for r in records).encode('utf-8')
        (temporary / 'files' / 'records.jsonl').write_bytes(body)
        entries = [{'path': 'records.jsonl', 'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()}]
        item = {'id': resource_id, 'name': str(name)[:120], 'files': entries, 'size_bytes': len(body),
                'graph_id': str(graph_id or ''), 'records': len(records),
                'version': hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest(),
                **({'origin': origin} if origin else {})}
        (temporary / 'manifest.json').write_text(json.dumps(item, ensure_ascii=False), encoding='utf-8')
        temporary.rename(path_for(resource_id))
        return public(item)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def public(item):
    return {**item, 'root': str((path_for(item['id']) / 'files').resolve())}


@router.get('')
def listing():
    resources = []
    for path in sorted(directory().glob('*/manifest.json')):
        if path.parent.name.startswith('.'):
            continue
        # One unreadable manifest must not hide every other resource.
        try:
            resources.append(public(json.loads(path.read_text(encoding='utf-8'))))
        except (OSError, ValueError, KeyError, TypeError, SourceError):
            continue
    return {'resources': resources}



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
        (temporary / 'manifest.json').write_text(json.dumps(item), encoding='utf-8')
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
    """Delete a resource no saved workflow uses.

    Under the association lock, so an attach cannot land between the in-use
    check and the delete (it would write its manifest into a folder being
    removed). The folder is renamed aside under the lock and removed after,
    so a large one does not hold attaches up."""
    try:
        with _association_lock:
            load(resource_id)
            used = _used_by(resource_id)
            if used:
                raise HTTPException(409, 'Resource is used by saved workflows: ' + ', '.join(used))
            trash = directory() / f'.deleted-{resource_id}-{uuid.uuid4().hex}'
            path_for(resource_id).rename(trash)
        shutil.rmtree(trash, ignore_errors=True)
        return {'deleted': resource_id}
    except SourceError as exc:
        raise HTTPException(404, str(exc)) from exc


def empty_trash():
    """Remove resource folders a delete renamed aside but did not finish
    removing (the server stopped, or a file was locked)."""
    root = data_path('data-resources')
    if root.is_dir():
        for trash in root.glob('.deleted-*'):
            if trash.is_dir() and not trash.is_symlink():
                shutil.rmtree(trash, ignore_errors=True)


def _used_by(resource_id):
    """Names of the saved workflows that reference the resource. A workflow
    file that cannot be read is skipped (and logged), not a 500: one corrupt
    file must not make every resource undeletable."""
    import logging
    from backend.api import graphs
    from backend.features.evaluation.evaluator_tools import label_resource_ids
    used = []
    for path in graphs.GRAPHS_DIR.glob('*.json'):
        try:
            graph = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(graph, dict):
                raise ValueError('not a workflow object')
            if resource_id in label_resource_ids(graph) or any(
                    isinstance(t, dict) and (t.get('source') or {}).get('resource_id') == resource_id
                    for t in graph.get('tasks') or []):
                used.append(graph.get('name') or graph.get('id') or path.stem)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            logging.getLogger(__name__).warning(
                'data resource %s: skipped unreadable workflow file %s: %s', resource_id, path, exc)
    return used


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
            write_json(path_for(resource_id) / 'manifest.json', item, indent=None)
        return public(item)
    except SourceError as exc:
        raise HTTPException(404, str(exc)) from exc
