"""Read-only workspace views of uploaded input resources, without copying bytes."""
import re
from pathlib import Path, PurePosixPath
from backend.features.data import data_resources

PREFIX = 'datasets'


def resources(graph_id):
    from backend.api import graphs
    graph = graphs.load_graph(graph_id) or {}
    referenced = {(t.get('source') or {}).get('resource_id') for t in graph.get('tasks', [])}
    from backend.features.evaluation.evaluator_tools import label_resource_ids
    referenced.update(label_resource_ids(graph))
    return [r for r in data_resources.listing()['resources'] if r.get('graph_id') == graph_id or graph_id in (r.get('workspace_graph_ids') or []) or r['id'] in referenced]


def mount_name(resource):
    name = re.sub(r'[^\w.-]+', '_', resource['name']).strip('._') or 'data'
    return f"{PREFIX}/{name}-{resource['id']}"


def entries(graph_id):
    result = []
    for resource in resources(graph_id):
        mount = mount_name(resource)
        root = (data_resources.path_for(resource['id']) / 'files').resolve()
        folders = {mount:root}
        for item in resource['files']:
            path = f"{mount}/{item['path']}"
            parent = PurePosixPath(item['path']).parent
            while str(parent) != '.':
                folders[f'{mount}/{parent}'] = root / str(parent)
                parent = parent.parent
            result.append({'path':path,'size':item['bytes'],'mtime':'','readonly':True,
                           'absolute_path':str(root / item['path']), 'resource_id':resource['id']})
        result.extend({'path':p,'dir':True,'readonly':True,'absolute_path':str(real)} for p,real in folders.items())
    if result:
        result.insert(0,{'path':PREFIX,'dir':True,'readonly':True})
    return result


def resolve(graph_id, path):
    if path != PREFIX and not path.startswith(PREFIX + '/'):
        return None
    from backend.api.workspace import WorkspaceError
    entry = next((e for e in entries(graph_id) if e['path'] == path), None)
    if not entry or not entry.get('absolute_path'):
        raise WorkspaceError('Choose a dataset folder or file in this workspace.', not_found=True)
    return Path(entry['absolute_path'])


def refuse(path):
    if path == PREFIX or path.startswith(PREFIX + '/'):
        from backend.api.workspace import WorkspaceError
        raise WorkspaceError('Uploaded datasets are immutable. Manage or remove the resource from Input, or upload a new version.')
