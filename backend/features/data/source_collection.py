"""Collect canvas inputs once, persist them, then run the saved records."""
import copy
import hashlib
import json
import threading
import uuid
import queue
from fastapi import APIRouter, Body, HTTPException
from backend.api import graphs, sources, chat_control, batch, preprocess, evaluation, tools_registry
from backend.api.studio_config import data_path
from backend.features.data import input_composition as composition

router = APIRouter()
_lock = threading.RLock()
_jobs = {}
_controls = {}

def directory():
    path = data_path('collections')
    path.mkdir(parents=True, exist_ok=True)
    return path

def node_for(graph):
    return composition.primary(graph)


def fingerprint(node, graph=None):
    """What the collected data depends on: the Input's source settings and
    those of its references — not where the node sits on the canvas or its
    description, which change nothing about the records."""
    refs = composition.references(graph, node) if graph else []
    key = {"name": node.get("name"), "source": node.get("source") or {}}
    if refs:
        key = {"primary": key, "references": [{"name": r.get("name"), "source": r.get("source") or {}} for r in refs]}
    return hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()

def _legacy_fingerprint(node, graph=None):
    # Collections saved before the fingerprint ignored layout stay usable.
    refs = composition.references(graph, node) if graph else []
    if refs:
        node = {"primary": node, "references": refs}
    return hashlib.sha256(json.dumps(node, sort_keys=True).encode()).hexdigest()


def matches(job, node, graph):
    return job.get('fingerprint') in (fingerprint(node, graph), _legacy_fingerprint(node, graph))


def save(job):
    path = directory() / (job['id'] + '.json')
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(job, ensure_ascii=False))
    temporary.replace(path)

def read(graph_id, collection_id):
    if not isinstance(collection_id, str) or len(collection_id) != 32 or any(c not in '0123456789abcdef' for c in collection_id):
        raise sources.SourceError('Invalid collection ID')
    with _lock:
        job = _jobs.get(collection_id)
        if job is None:
            path = directory() / (collection_id + '.json')
            if not path.exists():
                raise sources.SourceError('Collected data not found; collect the source again.')
            job = json.loads(path.read_text())
            if job['status'] == 'collecting':
                job.update(status='failed', error='Collection was interrupted by a service restart. Collect again.')
        if job['graph_id'] != graph_id:
            raise sources.SourceError('Collected data belongs to a different task.')
        return copy.deepcopy(job)

def records_for(graph, node, collection_id):
    job = read(graph['id'], collection_id)
    if not matches(job, node, graph):
        raise sources.SourceError('Input source changed. Collect again before running.')
    if job['status'] != 'ready':
        raise sources.SourceError('Input collection is not complete.')
    return job['records'], {'type': 'canvas', 'node': node['name'], 'collection_id': job['id'], 'config': job['config']}

def public(job):
    job = copy.deepcopy(job)
    live = {entry['id']: batch.get_batch(entry['id']) or {} for entry in job.get('batches', [])}
    job['batches'] = [{**entry, 'status': live[entry['id']].get('status', entry.get('status', 'pending')),
                       'token_usage': live[entry['id']].get('token_usage')} for entry in job.get('batches', [])]
    from backend.features.execution.token_usage import combined
    job['token_usage'] = combined(*(entry['token_usage'] for entry in job['batches']))
    return {k: v for k, v in job.items() if k not in ('records', 'fingerprint', 'config', 'reference_inputs')} | {
        'sample': job.get('records', [])[:3],
        'source': {key: job['config'].get(key) for key in ('type', 'query', 'days', 'start_date', 'end_date', 'batch_step')}}

def sequence_inputs(graph):
    """Keep the trajectory fields an Input declares through the mapping, so
    the batch can still group the records it runs."""
    declared = sources.input_sequence(graph) or {}
    return [{'name': declared[k], 'required': False} for k in ('group', 'order') if declared.get(k)]


def mapped_chunk(graph, node, records, metric, label_key):
    _, inputs = graphs.validate_graph(graph)
    inputs = [*inputs, *composition.all_outputs(graph), *sequence_inputs(graph)]
    records = preprocess.apply(graph, records)
    labels = None
    if metric:
        records, labels = evaluation.split_labels(records, label_key)
    mapped = sources.map_to_workflow_inputs(records, inputs)
    declared = {o['name'] for o in composition.all_outputs(graph)}
    if any(declared - r.keys() for r in mapped):
        raise sources.SourceError('Preprocessing removed source outputs; preserve them to avoid refetching.')
    return mapped, labels


def execute_collection(job, graph, node, control, workers, metric, label_key):
    token = chat_control.current.set(control)
    done = threading.Event()
    pending = queue.Queue()
    errors = []
    streaming = job['mode'] == 'stream'
    prepared = job['mode'] == 'prepare'
    run_graph = graph
    declared = {o['name'] for o in node.get('outputs', [])}
    import time
    last_save = [0.0]
    def receive(record):
        control.check()
        if not isinstance(record, dict) or declared - record.keys():
            raise sources.SourceError('Source record is missing declared outputs.')
        if node['source'].get('type') != 'dataloader':
            record = composition.merge([record], job.get('reference_inputs', {}))[0]
        with _lock:
            job['records'].append(record)
            job['record_count'] = len(job['records'])
            # Rewriting the whole job (every record so far) per record made a
            # long collection slower with each record and held the lock the
            # status poll needs. At most once a second; the end saves it all.
            if time.monotonic() - last_save[0] >= 1.0:
                save(job)
                last_save[0] = time.monotonic()
        if streaming:
            pending.put(record)
    def run_chunk(records):
        control.check()
        mapped, labels = mapped_chunk(run_graph, node, records, metric, label_key)
        source = {'type': 'canvas', 'node': node['name'], 'collection_id': job['id'],
                  'mode': job['mode'], 'chunk': len(job['batches']) + 1, 'config': job['config']}
        with _lock:
            control.check()
            id = batch.start_batch({**run_graph, '_defer_evaluators': True}, mapped, source, workers=workers, metric=metric, labels=labels, mode='node')
            job['batches'].append({'id': id, 'total': len(mapped)})
            job['submitted_records'] += len(records)
            save(job)
        try:
            while not batch.wait_for(id, timeout=.1):
                control.check()
            control.check()
            result = batch.get_batch(id) or {}
            if result.get('status') != 'succeeded':
                raise sources.SourceError('A batch did not succeed. Later batches were stopped to preserve record order; inspect the batch results.')
        except BaseException:
            batch.cancel_batch(id)
            raise
    def consume():
        buffer = []
        try:
            while True:
                control.check()
                try:
                    buffer.append(pending.get(timeout=.1))
                except queue.Empty:
                    if done.is_set():
                        break
                    continue
                if len(buffer) == job['batch_size']:
                    run_chunk(buffer)
                    buffer = []
            if buffer:
                run_chunk(buffer)
        except chat_control.Cancelled:
            pass
        except Exception as exc:
            errors.append(str(exc))
            control.event.set()
    consumer = threading.Thread(target=consume, daemon=True) if streaming else None
    if consumer:
        consumer.start()
    try:
        def stage(value):
            try:
                data = json.loads(value)
                with _lock:
                    job.update(completed=data['completed'], total=data['total'])
            except (ValueError, KeyError):
                pass
        worker_node = copy.deepcopy(node)
        if worker_node['source'].get('type') == 'dataloader':
            worker_node['source']['reference_inputs'] = job.get('reference_inputs', {})
        records = chat_control.worker('collect', worker_node, on_stage=stage, on_record=receive)
        # Compatibility with source workers that only return a final list.
        if not job['records']:
            for record in records:
                receive(record)
        if not job['records']:
            raise sources.SourceError('No records found. Adjust the query or date range.')
        with _lock:
            job['collection_complete'] = True
        # Keep what was fetched as a data resource: a DataLoader can read and
        # preprocess it in code, and re-runs use the same data instead of
        # fetching again.
        try:
            from backend.features.data import data_resources
            config = node['source'].get('source_config') or node['source']
            # Name it after whatever identifies this source type, not after
            # fields of one particular connector.
            from backend.features.data.source_apis import SOURCE_TYPE_SCHEMAS
            identifying = [field['name'] for field in (SOURCE_TYPE_SCHEMAS.get(config.get('type')) or {}).get('config', [])
                           if field.get('required')]
            label = next((str(config[key]) for key in identifying if str(config.get(key) or '').strip()), None) or config.get('type') or 'source'
            resource = data_resources.create_from_records(
                job['records'], f"Collected · {label} · {job['id'][:8]} · {len(job['records'])} records",
                graph.get('id'), origin={'collection_id': job['id'], 'source': copy.deepcopy(config)})
            with _lock:
                job['resource_id'], job['resource_name'] = resource['id'], resource['name']
                save(job)
        except Exception as exc:
            with _lock:
                job['resource_error'] = f"Collected records could not be saved as a data resource: {exc}"
        if prepared:
            control.check()
            with _lock:
                job['phase'] = 'preprocessing'
                save(job)
            cleaned = preprocess.apply_dataset(job.get('preprocess_tool'), job['records'])
            run_graph = {**graph, 'preprocess': None}
            # Validate the complete transformed set before any expensive batch starts.
            mapped_chunk(run_graph, node, cleaned, metric, label_key)
            with _lock:
                job['records'] = cleaned
                job['processed_count'] = len(cleaned)
                job['phase'] = 'running'
                save(job)
            for offset in range(0, len(cleaned), job['batch_size']):
                run_chunk(cleaned[offset:offset + job['batch_size']])
    except chat_control.Cancelled:
        pass
    except Exception as exc:
        errors.append(str(exc))
        control.event.set()
    finally:
        done.set()
        if consumer:
            consumer.join()
        # The collection is over here. What follows is its verdict and a
        # report over what ran, neither of which the stop may cut short: the
        # report runs in a worker of its own, and that worker watching the
        # same stop left the job reading `collecting` for ever.
        chat_control.current.reset(token)
        with _lock:
            if errors:
                job.update(status='failed', error=errors[0])
            elif control.event.is_set():
                job.update(status='cancelled', error='Stopped. Completed results are retained.'
                           if streaming or prepared else 'Collection stopped. No workflow was started.')
            else:
                job['status'] = 'completed' if streaming or prepared else 'ready'
            save(job)
        # Over whatever actually ran: a collection that was stopped part way
        # still produced batches, and their evaluator reports are what says
        # how that part went (a cancelled batch reports the same way).
        if (streaming or prepared) and job.get('batches'):
            from backend.api import runner
            from backend.features.evaluation.evaluator_tools import evaluate_runs
            runs = [(runner.get_run(item['run_id']) if item.get('run_id') else None) or {**item, 'nodes': []}
                    for entry in job.get('batches', [])
                    for item in (batch.get_batch(entry['id']) or {}).get('items', [])]
            try:
                job['evaluations'] = evaluate_runs(graph, [run for run in runs if run], timing={'batch'})
            except Exception as exc:
                job['evaluation_error'] = str(exc)
        with _lock:
            save(job)
            _controls.pop(job['id'], None)
            _jobs.pop(job['id'], None)


@router.post('/api/graphs/{graph_id}/source-collections')
def collect(graph_id: str, body: dict = Body(default={})):
    graph = graphs.load_graph(graph_id)
    if not graph:
        raise HTTPException(404, 'Task not found')
    try:
        node = copy.deepcopy(node_for(graph))
        original = fingerprint(node, graph)
        from backend.features.execution import provider_batch
        try:
            from backend.features.execution.batch_settings import loader_batch_size
            shared_size = loader_batch_size({'config':node['source']})
            requested = body.get('llm_batch_size')
            size = provider_batch.validate(graph, shared_size if shared_size is not None and requested not in (None, '') else requested)
        except ValueError as exc:
            raise sources.SourceError(str(exc)) from exc
        if size is not None:
            graph = {**graph, '_llm_batch_size': size}
        mode = body.get('mode', 'all')
        if mode not in ('all', 'stream', 'prepare'):
            raise sources.SourceError('Choose collect-all, streaming, or preprocess-all then batch mode.')
        try:
            batch_size = shared_size if shared_size is not None else int(body.get('batch_size', 10))
            workers = int(body.get('workers', 2))
        except (ValueError, TypeError) as exc:
            raise sources.SourceError('Batch size and workers must be whole numbers.') from exc
        if not 1 <= batch_size <= (1024 if shared_size is not None else 1000) or not 1 <= workers <= batch.MAX_WORKERS:
            raise sources.SourceError('Batch size must be 1–1000; workers must be 1–64.')
        metric, label_key = body.get('metric') or None, body.get('label_key') or None
        if mode in ('stream', 'prepare'):
            graphs.validate_graph(graph)
            from backend.api.app import _graph_tool_names
            tools_registry.validate_tool_names(_graph_tool_names(graph))
        whole_tool = body.get('preprocess_tool') or ''
        if mode == 'prepare':
            found = preprocess.custom_tools.find(whole_tool) if isinstance(whole_tool, str) else None
            if not found:
                raise sources.SourceError('Choose an existing whole-dataset preprocessing tool.')
            preprocess._single_param(found[1])
        # Range options apply to the API source itself, also when a DataLoader wraps it.
        target = (node['source'].get('source_config') if node['source']['type'] == 'dataloader'
                  else node['source'])
        from backend.features.data.source_apis import SOURCE_TYPE_SCHEMAS
        declared = {field['name'] for field in (SOURCE_TYPE_SCHEMAS.get((target or {}).get('type')) or {}).get('config', [])}
        for key in ('start_date', 'end_date', 'batch_step'):
            if key in body and key in declared:
                target[key] = body[key]
        job = {'id': uuid.uuid4().hex, 'graph_id': graph_id, 'fingerprint': original,
               'preprocess_tool': whole_tool, 'phase': 'collecting', 'llm_batch_size': size,
               'config': node['source'], 'reference_inputs': composition.snapshot(graph, node), 'status': 'collecting', 'completed': 0, 'total': None,
               'record_count': 0, 'records': [], 'error': None, 'mode': mode,
               'batch_size': batch_size, 'batches': [], 'submitted_records': 0, 'collection_complete': False}
        with _lock:
            _jobs[job['id']] = job
            save(job)
            control = chat_control.Control()
            _controls[job['id']] = control
        threading.Thread(target=execute_collection, args=(job, copy.deepcopy(graph), node, control, workers, metric, label_key), daemon=True).start()
        return public(job)
    except (sources.SourceError, preprocess.PreprocessError, graphs.GraphValidationError, tools_registry.ToolResolveError) as exc:
        raise HTTPException(422, str(exc)) from exc

@router.get('/api/graphs/{graph_id}/source-collections/{collection_id}')
def status(graph_id: str, collection_id: str):
    try:
        return public(read(graph_id, collection_id))
    except sources.SourceError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post('/api/graphs/{graph_id}/source-collections/{collection_id}/stop')
def stop(graph_id: str, collection_id: str):
    try:
        job = read(graph_id, collection_id)
        with _lock:
            control = _controls.get(collection_id)
            if control:
                control.event.set()
        return {'status': 'stopping' if control else job['status']}
    except sources.SourceError as exc:
        raise HTTPException(422, str(exc)) from exc
