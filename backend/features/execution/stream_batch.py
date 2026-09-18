"""Execute streamed input chunks under one batch ID; archive full inputs to disk."""
import json
from . import batch
from backend.api import runner


def execute_stream(batch_id, graph, chunks, workers):
    state=batch._batches[batch_id]
    iterator=None
    try:
        iterator=chunks(lambda: state.get('cancel_requested',False))
        for records, labels in iterator:
            if state.get('cancel_requested'): break
            pairs=[]
            for i,record in enumerate(records):
                index=len(state['items'])
                relative=f'{batch_id}-inputs/{index}.json'
                path=batch.BATCHES_DIR/relative;path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text(json.dumps(record,ensure_ascii=False,allow_nan=False))
                item={'index':index,'status':'pending','run_id':None,'inputs':record,'input_file':relative,
                      'output_summary':None,'error':None,'review_status':None,'label':labels[i] if labels else None,'score':None,'score_detail':None}
                with batch._lock: state['items'].append(item);state['total']=len(state['items'])
                pairs.append((item,record))
            batch._execute_batch(batch_id,graph,pairs,workers,finalize=False)
            # Full inputs/results remain in per-run and input files, not in a growing RAM list.
            failed=any(item['status'] != 'success' for item,_ in pairs)
            for item,record in pairs:
                item['inputs']={k:v for k,v in record.items() if k in ('sample_id','as_of','_dataloader') or isinstance(v,(str,int,float,bool,type(None))) and len(str(v)) <= 160}
                item['inputs_archived']=True
                if item.get('run_id'):
                    with runner._lock: runner._runs.pop(item['run_id'],None)
            batch._persist_batch(state)
            if failed:
                state['error']='A streamed chunk did not succeed. Reading stopped to preserve execution order; full inputs are archived.'
                break
        else:
            state['collection_complete']=not state.get('cancel_requested',False)
        if not state['items'] and not state.get('cancel_requested'):
            raise ValueError('DataLoader produced no records.')
        batch._finish_batch(state,graph)
    except Exception as exc:
        with batch._lock:
            state.update(status='cancelled' if state.get('cancel_requested') else 'failed',error=str(exc))
            batch._persist_batch(state)
    finally:
        if iterator is not None: iterator.close()
