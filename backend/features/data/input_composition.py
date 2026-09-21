"""One record stream plus reusable reference datasets, shared by all run modes."""
import copy
from backend.api import sources


def is_reference(node):
    config = node.get('source') or {}
    return config.get('type') in ('user_dataset', 'dataloader') and config.get('input_mode') == 'reference'


def connected(graph):
    wired = {e.get('source') for e in graph.get('edges', [])}
    return [n for n in sources.find_source_nodes(graph)
            if n.get('name') in wired and n.get('enabled', True)]


def primary(graph):
    nodes = connected(graph)
    streams = [n for n in nodes if not is_reference(n)]
    if len(streams) == 1:
        return streams[0]
    if len(nodes) == 1:
        return nodes[0]
    raise sources.SourceError('Choose one per-record Input. Set the other datasets to Shared reference (a list every record should see).')


def references(graph, main=None):
    return [n for n in connected(graph) if is_reference(n) and n.get('name') != (main or {}).get('name')]


def snapshot(graph, main=None):
    result = {}
    for node in references(graph, main):
        row = sources.records_from_source_node(node)[0]
        if result.keys() & row.keys():
            raise sources.SourceError('Shared inputs have duplicate output names. Give each reference a unique output name.')
        result.update(row)
    return result


def merge(records, reference_inputs):
    if any(row.keys() & reference_inputs.keys() for row in records):
        raise sources.SourceError('News/record fields overlap shared reference output names. Rename the reference output to avoid overwriting data.')
    return [{**row, **copy.deepcopy(reference_inputs)} for row in records]


def all_outputs(graph):
    return [{'name': o['name'], 'required': False}
            for node in connected(graph) for o in node.get('outputs', [])]


def load_primary(graph, main=None):
    """Reference data is available inside a DataLoader transform, not only after it."""
    main = main or primary(graph)
    refs = snapshot(graph, main)
    config = main.get('source') or {}
    if config.get('type') == 'dataloader' and not is_reference(main):
        from .dataloaders import records
        return records({**config, 'reference_inputs': refs})
    return merge(sources.records_from_source_node(main), refs)
