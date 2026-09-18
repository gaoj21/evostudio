"""Start a configured DataLoader without collecting the dataset in the HTTP request."""
import copy
from backend.api import graphs, sources, batch
from backend.features.data import input_composition, dataset_stream, source_collection
from . import provider_batch
from .batch_settings import loader_batch_size


def start(graph, node, body):
    graph=copy.deepcopy(graph)
    graphs.validate_graph(graph)
    config=copy.deepcopy(node['source'])
    source={'type':'canvas','node':node['name'],'config':config}
    size=loader_batch_size(source)
    requested=body.get('llm_batch_size')
    native=provider_batch.validate(graph,size if requested is not None else None)
    if native is not None: graph['_llm_batch_size']=native
    from backend.api.app import _gray_zone
    zone=_gray_zone(body.get('review_zone'))
    workers=int(body.get('workers',2))
    if not 1 <= workers <= batch.MAX_WORKERS: raise sources.SourceError('Invalid worker count.')
    period=body.get('period','none')
    date_field=body.get('date_field') or 'as_of'
    if period not in ('none','daily','weekly','monthly'): raise sources.SourceError('Invalid execution period.')
    source.update(period=period, date_field=date_field)
    def chunks(cancelled):
        refs=input_composition.snapshot(graph,node)
        stream=dataset_stream.chunks({**config,'reference_inputs':refs},cancelled)
        try:
            for rows in period_chunks(stream,size,period,date_field):
                if refs: rows=input_composition.merge(rows,refs)
                yield source_collection.mapped_chunk(graph,node,rows,body.get('metric'),body.get('label_key'))
        finally: stream.close()
    bid=batch.start_batch(graph,[],source,gray_zone=zone,workers=workers,metric=body.get('metric'),record_chunks=chunks)
    return {'batch_id':bid,'total':None,'streaming':True}


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
                day=date.fromisoformat(str(get_path(row,date_field))[:10])
            except (ValueError,TypeError,KeyError) as exc:
                raise sources.SourceError(f'Execution grouping needs an ISO date in {date_field}.') from exc
            if previous is not None and day < previous:
                raise sources.SourceError('Date grouping requires chronological Dataset output. Sort or order records in your DataLoader.')
            key=day if period == 'daily' else day.isocalendar()[:2] if period == 'weekly' else (day.year,day.month)
            if buffer and key != current:
                yield buffer;buffer=[]
            current,previous=key,day
            buffer.append(row)
            if len(buffer)==size: yield buffer;buffer=[]
    if buffer: yield buffer
