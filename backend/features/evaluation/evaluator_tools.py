"""Canvas evaluators: the same contract for runs, saved results and evolution.

Evaluation branches never feed workflow nodes. Reports are kept separately
from predictions; unavailable labels are unscored, never an implicit zero.
"""
import copy
import json
import math
from statistics import mean
from fastapi import APIRouter, Body, HTTPException
from backend.api.sources import SourceError

router = APIRouter(prefix='/api/evaluators', tags=['Evaluators'])
CATALOG = [
    {'id': 'python', 'label': 'Python evaluator', 'metric': 'score', 'direction': 'maximize'},
    {'id': 'exact_match', 'label': 'Exact match', 'metric': 'accuracy', 'direction': 'maximize'},
    {'id': 'contains', 'label': 'Contains expected text', 'metric': 'match_rate', 'direction': 'maximize'},
    {'id': 'required_fields', 'label': 'Required JSON fields', 'metric': 'valid_rate', 'direction': 'maximize'},
    {'id': 'tool', 'label': 'Custom evaluator tool', 'metric': 'score', 'direction': 'maximize'},
]


def is_evaluator(task):
    return task.get('kind') == 'evaluator'


def validate_graph(graph):
    tasks = {t['name']: t for t in graph.get('tasks', [])}
    for task in tasks.values():
        if not is_evaluator(task):
            continue
        cfg = task.get('evaluator') or {}
        if cfg.get('type', 'exact_match') not in {e['id'] for e in CATALOG}:
            raise SourceError('Unknown evaluator type.')
        if cfg.get('timing', 'run') not in ('node', 'run', 'batch'):
            raise SourceError('Evaluator timing must be node, run, or batch.')
        if cfg.get('type') == 'python':
            from .python_evaluator import interface
            try:
                interface(cfg.get('code') or '')
            except (ValueError, TypeError) as exc:
                raise SourceError(str(exc)) from exc
        if cfg.get('type') == 'tool' and not cfg.get('tool'):
            raise SourceError('Choose an evaluator tool.')
        if cfg.get('direction', 'maximize') not in ('maximize', 'minimize'):
            raise SourceError('Evaluator direction must be maximize or minimize.')
    if any(is_evaluator(tasks.get(e.get('source'), {})) for e in graph.get('edges', [])):
        raise SourceError('Evaluator outputs are reports, not workflow inputs. Select an evaluator in Evolve instead of connecting it downstream.')


def decode(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def path_value(value, path):
    from backend.features.data.dataloaders import get_path
    try:
        return get_path(decode(value), path)
    except (KeyError, TypeError, IndexError):
        return None


def report(cfg, records):
    """Pass complete saved executions to the tool, without grouping or filtering."""
    cfg, records = copy.deepcopy(cfg), copy.deepcopy(records)
    kind = cfg.get('type', 'exact_match')
    if kind == 'tool' and cfg.get('tool') in ('read_dataset','evaluate_records'):
        raise SourceError('Choose a non-recursive custom evaluator.')
    if kind in ('tool', 'python'):
        if kind == 'python':
            from backend.features.chat import chat_control
            result = chat_control.worker('evaluate_python', {'code':cfg['code'], 'records':records, 'config':cfg.get('config') or {}}, timeout=120)
        else:
            from backend.api import tools_registry
            result = tools_registry.call_tool(cfg['tool'], {'records': records, 'config': cfg.get('config') or {}})
        if not isinstance(result, dict) or not isinstance(result.get('metrics'), dict):
            raise SourceError('Evaluator tools return {metrics: {name: number}} with optional records, coverage and details.')
        result = copy.deepcopy(result)
        metric = cfg.get('metric') or (next(iter(result['metrics']), 'score') if cfg.get('_preview') else 'score')
        result['objective'] = {'metric': metric, 'direction': cfg.get('direction', 'maximize')}
        if metric not in result['metrics']:
            raise SourceError(f'Evaluator did not return objective metric {metric!r}.')
        if any(v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)) for v in result['metrics'].values()):
            raise SourceError('Evaluator metrics must be finite numbers or null.')
        if 'records' in result and (not isinstance(result['records'], list) or any(not isinstance(r, dict) for r in result['records'])):
            raise SourceError('Evaluator records must be a list of objects; their granularity is chosen by the tool.')
        for row in result.get('records', []):
            value = row.get('score')
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
                raise SourceError('Evaluator scores must be finite numbers or null.')
        coverage = result.get('coverage')
        if coverage is not None:
            if not isinstance(coverage, dict) or not isinstance(coverage.get('unit'), str) or not coverage['unit'].strip():
                raise SourceError('Tool coverage must declare its unit.')
            counts = [coverage.get(k) for k in ('total', 'scored', 'unscored')]
            if any(type(v) is not int or v < 0 for v in counts) or counts[1] + counts[2] != counts[0]:
                raise SourceError('Coverage needs consistent nonnegative total, scored and unscored counts.')
        return result
    details = []
    for row in records:
        score, reason = None, 'Missing expected value'
        prediction = path_value(row.get('prediction'), cfg.get('prediction_path'))
        expected = row.get('expected')
        if row.get('status', 'success') != 'success':
            reason = 'Execution did not succeed'
        elif prediction is None:
            reason = 'Prediction is unavailable'
        elif kind == 'required_fields':
            fields = cfg.get('required_fields') or []
            if not fields:
                raise SourceError('Specify at least one required field.')
            missing = [f for f in fields if path_value(prediction, f) is None]
            score, reason = float(not missing), 'Missing: ' + ', '.join(missing) if missing else 'Required fields present'
        elif expected is not None:
            if kind == 'exact_match':
                score = float(decode(prediction) == decode(expected))
            elif kind == 'contains':
                score = float(str(expected) in str(prediction))
            else:
                raise SourceError('Unknown evaluator.')
            reason = 'Matched' if score else 'Did not match expected value'
        details.append({'id': row['id'], 'score': score, 'reason': reason})
    scores = [r['score'] for r in details if r['score'] is not None]
    metric = next(e['metric'] for e in CATALOG if e['id'] == kind)
    return {'metrics': {metric: mean(scores) if scores else None}, 'records': details,
            'coverage': {'total': len(details), 'scored': len(scores), 'unscored': len(details)-len(scores)},
            'objective': {'metric': metric, 'direction': 'maximize'}}


def record_for(graph, task, run, record_id=None):
    values = {n['name']: n.get('output') for n in run.get('nodes', [])}
    values.update(run.get('node_outputs') or {})
    inputs = run.get('inputs') or run.get('_effective_inputs') or {}
    mapped = {}
    for edge in graph.get('edges', []):
        if edge.get('target') != task['name']:
            continue
        output = decode(values.get(edge.get('source')))
        for mapping in edge.get('mappings', []):
            mapped[mapping['to']] = path_value(output, mapping['from'])
    cfg = task.get('evaluator') or {}
    expected = mapped.get('expected')
    if cfg.get('label_field'):
        expected = path_value(inputs, cfg['label_field'])
    # Keep every saved field, including errors, provenance and execution snapshot.
    # Mappings are conveniences, never an access filter on other node outputs.
    record = copy.deepcopy({k: v for k, v in run.items() if not k.startswith('_')})
    record.update({'id': record_id or run.get('id') or run.get('run_id', 'record'),
                   'inputs': copy.deepcopy(inputs), 'node_outputs': copy.deepcopy(values),
                   'prediction': mapped.get('prediction', run.get('result', run.get('prediction'))),
                   'expected': expected, 'status': run.get('status', 'success'),
                   'focus': copy.deepcopy([e for e in graph.get('edges', []) if e.get('target') == task['name']])})
    return record


def evaluate_runs(graph, runs, names=None, timing=None):
    validate_graph(graph)
    reports = {}
    for task in graph.get('tasks', []):
        if not is_evaluator(task) or not task.get('enabled', True) or (names is not None and task['name'] not in names):
            continue
        cfg = task.get('evaluator') or {}
        if timing and cfg.get('timing', 'run') not in timing:
            continue
        try:
            records = [record_for(graph, task, run, str(i)) for i, run in enumerate(runs)]
            # Labels are a separate resource and are never injected into Agents.
            if cfg.get('labels') or '_label_records' in cfg:
                from backend.features.data.dataloaders import records as load_records
                labels = cfg['_label_records'] if '_label_records' in cfg else load_records(cfg['labels'])
                if cfg.get('type') in ('tool', 'python'):
                    # Tools own label matching too; no platform-imposed join or grouping.
                    cfg = copy.deepcopy(cfg)
                    cfg.setdefault('config', {})
                    cfg['config']['label_records'] = copy.deepcopy(labels)
                    labels = []
                key = cfg.get('label_key') or 'id'
                value = cfg.get('label_value') or 'expected'
                lookup = {}
                for label in labels:
                    identifier = path_value(label, key)
                    if identifier is None or str(identifier) in lookup:
                        raise SourceError('Label keys must be present and unique.')
                    lookup[str(identifier)] = path_value(label, value)
                for row in ([] if cfg.get('type') in ('tool', 'python') else records):
                    identifier = path_value(row['inputs'], cfg.get('record_key') or 'id')
                    row['expected'] = lookup.get(str(identifier)) if identifier is not None else None
            reports[task['name']] = {'status': 'success', **report(cfg, records), 'config': copy.deepcopy(cfg)}
        except Exception as exc:
            reports[task['name']] = {'status': 'failed', 'error': str(exc), 'config': copy.deepcopy(cfg)}
    return reports


@router.get('')
def catalog():
    return {'evaluators': CATALOG}


def saved_runs(graph_id, body):
    from backend.api import runner, batch
    if body.get('batch_id'):
        saved = batch.get_batch(body['batch_id'])
        if not saved or saved.get('graph_id') != graph_id:
            raise HTTPException(404, 'Batch not found in this workflow.')
        if saved.get('status') in ('running', 'pending', 'cancelling'):
            raise HTTPException(409, 'Wait for the batch to finish or stop it first.')
        return [(runner.get_run(i.get('run_id')) if i.get('run_id') else None) or {**i, 'nodes': []} for i in saved['items']]
    run = runner.get_run(body.get('run_id', ''))
    if not run or run.get('graph_id') != graph_id:
        raise HTTPException(404, 'Run not found in this workflow.')
    if run.get('status') in ('running', 'pending', 'cancelling'):
        raise HTTPException(409, 'Wait for execution to finish.')
    return [run]


@router.post('/interface')
def inspect_code(body: dict = Body(...)):
    from .python_evaluator import interface
    try:
        return interface(body.get('code') or '')
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post('/graphs/{graph_id}/preview')
def preview_code(graph_id: str, body: dict = Body(...)):
    from backend.api import graphs
    saved = graphs.load_graph(graph_id)
    if not saved:
        raise HTTPException(404, 'Workflow not found.')
    graph = copy.deepcopy(body.get('graph') or saved)
    name = body.get('evaluator')
    task = next((t for t in graph.get('tasks', []) if t.get('name') == name and is_evaluator(t)), None)
    if not task:
        raise HTTPException(422, 'Choose an evaluator on this canvas.')
    task['enabled'] = True
    task.setdefault('evaluator', {})['_preview'] = True
    # Other unfinished evaluator drafts must not block this preview.
    graph['tasks'] = [t for t in graph['tasks'] if not is_evaluator(t) or t['name'] == name]
    runs = saved_runs(graph_id, body)
    try:
        result = evaluate_runs(graph, runs, [name])[name]
    except (SourceError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    if result['status'] != 'success':
        raise HTTPException(422, result['error'])
    result.pop('config', None)
    return {'report':result, 'execution_count':len(runs),
            'metrics':[{'name':key,'type':'number','nullable':value is None,'sample':value} for key,value in result['metrics'].items()]}


@router.post('/graphs/{graph_id}/saved')
def evaluate_saved(graph_id: str, body: dict = Body(...)):
    from backend.api import graphs
    graph = graphs.load_graph(graph_id)
    if not graph:
        raise HTTPException(404, 'Workflow not found.')
    runs = saved_runs(graph_id, body)
    try:
        return {'evaluations': evaluate_runs(graph, runs, body.get('evaluators'))}
    except SourceError as exc:
        raise HTTPException(422, str(exc)) from exc
