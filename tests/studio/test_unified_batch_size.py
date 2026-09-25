from backend.api import batch, runner
from backend.features.execution import provider_batch


def setup(monkeypatch, fail_first=False):
    chunks, calls, runs, sizes = [], [], {}, []
    class Pool:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def map(self, function, groups):
            chunks.append(sum(len(g) for g in groups))
            return [function(g) for g in groups]
    def start(graph, record, **kwargs):
        calls.append(record['value'])
        runs[kwargs['run_id']] = {'status':'failed' if fail_first and record['value']==0 else 'success', 'result':record}
    monkeypatch.setattr(batch, 'ThreadPoolExecutor', Pool)
    monkeypatch.setattr(runner, 'start_run', start)
    monkeypatch.setattr(runner, 'get_run', lambda id: runs[id])
    monkeypatch.setattr(provider_batch, 'validate', lambda graph, size: sizes.append(size) or size)
    return chunks, calls, sizes


def test_input_size_controls_execution_chunks_native_size_and_tail(monkeypatch):
    chunks,calls,sizes=setup(monkeypatch)
    graph={'id':'unified','tasks':[], 'edges':[], '_llm_batch_size':99}
    source={'config':{'type':'dataloader','read_batch_size':2}}
    id=batch.start_batch(graph,[{'value':i} for i in range(5)],source)
    assert batch.wait_for(id,5)
    state=batch.get_batch(id)
    assert chunks==[2,2,1]
    assert calls==[0,1,2,3,4]
    assert sizes==[2]
    assert state['llm_batch_size']==state['batch_size']==2
    assert state['status']=='succeeded'


def test_failed_group_remains_blocked_across_chunk_boundaries(monkeypatch):
    chunks,calls,_=setup(monkeypatch,fail_first=True)
    records=[{'value':i,'_dataloader':{'group':'same','record_id':str(i)}} for i in range(3)]
    id=batch.start_batch({'id':'unified','tasks':[], 'edges':[]},records,{'config':{'type':'dataloader','read_batch_size':1}})
    assert batch.wait_for(id,5)
    state=batch.get_batch(id)
    assert calls==[0]
    assert [i['status'] for i in state['items']]==['failed','blocked','blocked']
    assert state['llm_batch_size'] is None


def test_a_finished_batch_never_evaluates_itself(monkeypatch):
    from backend.features.evaluation import evaluator_tools
    setup(monkeypatch)
    called = []
    monkeypatch.setattr(evaluator_tools, 'evaluate_runs', lambda *a, **kw: called.append(a) or {})
    id = batch.start_batch({'id': 'unified', 'tasks': [], 'edges': [],
                            'evaluators': [{'name': 'quality', 'timing': 'batch', 'code': 'x'}]}, [{'value': 1}], {})
    assert batch.wait_for(id, 5)
    finished = batch.get_batch(id)
    assert finished['status'] == 'succeeded'
    assert called == [] and not finished.get('evaluations')
