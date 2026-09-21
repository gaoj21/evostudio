"""Execute streamed input chunks under one batch ID; archive full inputs to disk.

A record that fails does not stop the stream. Records that must follow it
(the same memory entity, the same DataLoader group) are marked blocked by
the batch; everything else keeps running. A stopped or interrupted stream
can be resumed: unfinished records run first, in order, then reading
continues where it stopped.
"""
import json
from . import batch
from backend.api import runner


def _archive(state, batch_id, records, labels):
    pairs = []
    for i, record in enumerate(records):
        index = len(state['items'])
        relative = f'{batch_id}-inputs/{index}.json'
        path = batch.BATCHES_DIR / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, ensure_ascii=False, allow_nan=False))
        item = {'index': index, 'status': 'pending', 'run_id': None, 'inputs': record, 'input_file': relative,
                'output_summary': None, 'error': None, 'review_status': None,
                'label': labels[i] if labels else None, 'score': None, 'score_detail': None}
        with batch._lock:
            state['items'].append(item)
            state['total'] = len(state['items'])
        pairs.append((item, record))
    return pairs


def _release(pairs):
    # Full inputs stay in the per-item input files and per-run records; the
    # batch keeps only what identifies a record in a list.
    for item, record in pairs:
        item['inputs'] = {k: v for k, v in record.items()
                          if k == '_dataloader' or isinstance(v, (str, int, float, bool, type(None))) and len(str(v)) <= 160}
        item['inputs_archived'] = True
        if item.get('run_id'):
            with runner._lock:
                runner._runs.pop(item['run_id'], None)


def _consume(batch_id, graph, iterator, workers):
    state = batch._batches[batch_id]
    for chunk in iterator:
        records, labels = chunk[0], chunk[1]
        read = chunk[2] if len(chunk) > 2 else len(records)
        if state.get('cancel_requested'):
            break
        pairs = _archive(state, batch_id, records, labels)
        with batch._lock:
            state['records_read'] = state.get('records_read', 0) + read
        batch._execute_batch(batch_id, graph, pairs, workers, finalize=False)
        _release(pairs)
        batch._persist_batch(state)
    else:
        state['collection_complete'] = not state.get('cancel_requested', False)


def _run(batch_id, graph, workers, chunks, rerun=()):
    state = batch._batches[batch_id]
    iterator = None
    try:
        if rerun:
            batch._execute_batch(batch_id, graph, list(rerun), workers, finalize=False)
            _release(rerun)
            batch._persist_batch(state)
        if chunks is not None and not state.get('cancel_requested'):
            iterator = chunks(lambda: state.get('cancel_requested', False))
            _consume(batch_id, graph, iterator, workers)
        elif chunks is None:
            state['collection_complete'] = True
        if not state['items'] and not state.get('cancel_requested'):
            raise ValueError('DataLoader produced no records.')
        batch._finish_batch(state, graph)
    except Exception as exc:
        with batch._lock:
            state.update(status='cancelled' if state.get('cancel_requested') else 'failed', error=str(exc))
            batch._persist_batch(state)
    finally:
        if iterator is not None:
            iterator.close()


def execute_stream(batch_id, graph, chunks, workers):
    _run(batch_id, graph, workers, chunks)


def resume_stream(batch_id, graph, pairs, chunks, workers):
    """Unfinished records first, then the rest of the Dataset (if unread)."""
    _run(batch_id, graph, workers, chunks, rerun=pairs)
