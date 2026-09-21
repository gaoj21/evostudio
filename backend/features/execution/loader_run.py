"""Start or continue a configured DataLoader run without reading the dataset
in the HTTP request."""
import copy
from backend.api import graphs, sources, batch
from backend.features.data import input_composition, dataset_stream, source_collection
from . import provider_batch
from .batch_settings import loader_batch_size

PERIODS = ('none', 'daily', 'weekly', 'monthly')


def period_settings(body):
    """Execution grouping and the field it is keyed on. No default field:
    which field holds the date is a property of the user's data."""
    period = body.get('period', 'none')
    if period not in PERIODS:
        raise sources.SourceError('Invalid execution period.')
    date_field = (body.get('date_field') or '').strip()
    if period != 'none' and not date_field:
        raise sources.SourceError('Choose the date field that grouping by day, week or month should use.')
    return period, date_field


def chunk_source(graph, node, config, size, period, date_field, metric, label_key, skip=0):
    """A generator factory over the Dataset's records, starting after `skip`
    records already read. Yields (mapped records, labels, raw records read)."""
    config = copy.deepcopy(config)
    if skip:
        config['offset'] = config.get('offset', 0) + skip
        if config.get('n'):
            if config['n'] - skip <= 0:
                return None
            config['n'] -= skip

    def chunks(cancelled):
        refs = input_composition.snapshot(graph, node)
        stream = dataset_stream.chunks({**config, 'reference_inputs': refs}, cancelled)
        try:
            for rows in period_chunks(stream, size, period, date_field):
                read = len(rows)
                if refs:
                    rows = input_composition.merge(rows, refs)
                mapped, labels = source_collection.mapped_chunk(graph, node, rows, metric, label_key)
                yield mapped, labels, read
        finally:
            stream.close()
    return chunks


def start(graph, node, body):
    graph = copy.deepcopy(graph)
    graphs.validate_graph(graph)
    config = copy.deepcopy(node['source'])
    source = {'type': 'canvas', 'node': node['name'], 'config': config}
    size = loader_batch_size(source)
    requested = body.get('llm_batch_size')
    native = provider_batch.validate(graph, size if requested is not None else None)
    if native is not None:
        graph['_llm_batch_size'] = native
    from backend.api.app import _gray_zone
    zone = _gray_zone(body.get('review_zone'))
    workers = int(body.get('workers', 2))
    if not 1 <= workers <= batch.MAX_WORKERS:
        raise sources.SourceError('Invalid worker count.')
    period, date_field = period_settings(body)
    metric, label_key = body.get('metric'), body.get('label_key')
    # Kept with the batch so a Resume can continue reading the same way.
    source.update(period=period, date_field=date_field, metric=metric, label_key=label_key)
    chunks = chunk_source(graph, node, config, size, period, date_field, metric, label_key)
    bid = batch.start_batch(graph, [], source, gray_zone=zone, workers=workers,
                            metric=metric, record_chunks=chunks)
    return {'batch_id': bid, 'total': None, 'streaming': True}


def resume_chunks(graph, state):
    """The part of a stopped streaming batch's Dataset it had not read yet,
    or None when everything was read."""
    source = state.get('source') or {}
    node = next((t for t in graph.get('tasks') or [] if t.get('name') == source.get('node')), None)
    if node is None:
        raise sources.SourceError(f"The Input '{source.get('node')}' this batch read from is no longer in the workflow.")
    config = source.get('config') or node['source']
    size = loader_batch_size({'config': config})
    read = state.get('records_read')
    if read is None:
        read = len(state.get('items') or [])
    return chunk_source(graph, node, config, size, source.get('period', 'none'),
                        source.get('date_field') or '', source.get('metric'),
                        source.get('label_key'), skip=read)


def period_chunks(stream, size, period, date_field):
    """Bounded grouping of already ordered data; never aggregate or sort the dataset."""
    if period == 'none':
        yield from stream
        return
    from datetime import date
    from backend.features.data.dataloaders import get_path
    buffer, current, previous = [], None, None
    for chunk in stream:
        for row in chunk:
            try:
                day = date.fromisoformat(str(get_path(row, date_field))[:10])
            except (ValueError, TypeError, KeyError) as exc:
                raise sources.SourceError(f'Execution grouping needs an ISO date in {date_field}.') from exc
            if previous is not None and day < previous:
                raise sources.SourceError('Date grouping requires chronological Dataset output. Sort or order records in your DataLoader.')
            key = day if period == 'daily' else day.isocalendar()[:2] if period == 'weekly' else (day.year, day.month)
            if buffer and key != current:
                yield buffer
                buffer = []
            current, previous = key, day
            buffer.append(row)
            if len(buffer) == size:
                yield buffer
                buffer = []
    if buffer:
        yield buffer
