"""Evaluators: the user's own Python code, kept with the workflow document.

Evaluation is not part of the canvas. A workflow carries a list of evaluators
on its graph document (`graph["evaluators"]`), each one a piece of Python whose
typed parameters become the form that feeds it. Reports are kept separately
from predictions; unavailable labels are unscored, never an implicit zero.
"""
import copy
import json
import math
import re
from statistics import mean
from fastapi import APIRouter, Body, HTTPException
from backend.api.sources import SourceError
from backend.api.studio_config import DATA_DIR

router = APIRouter(prefix='/api', tags=['Evaluators'])

# Where a pasted-but-unsaved evaluator is kept, one draft per workflow, so
# leaving the panel to copy a run id from elsewhere does not lose the code.
DRAFTS_DIR = DATA_DIR / 'evaluator_drafts'

# Legacy fixed scorers. No evaluator can be created with them any more; they
# remain because `report()` is also the `evaluate_records` library tool, whose
# callers pass a scorer configuration of their own.
CATALOG = [
    {'id': 'python', 'label': 'Python evaluator', 'metric': 'score', 'direction': 'maximize'},
    {'id': 'exact_match', 'label': 'Exact match', 'metric': 'accuracy', 'direction': 'maximize', 'legacy': True},
    {'id': 'contains', 'label': 'Contains expected text', 'metric': 'match_rate', 'direction': 'maximize', 'legacy': True},
    {'id': 'required_fields', 'label': 'Required JSON fields', 'metric': 'valid_rate', 'direction': 'maximize', 'legacy': True},
    {'id': 'tool', 'label': 'Custom evaluator tool', 'metric': 'score', 'direction': 'maximize', 'legacy': True},
]

# One default, used by the runner, the batch and the panel alike.
DEFAULT_TIMING = 'run'
DEFAULT_TIMEOUT = 120
TIMINGS = ('manual', 'run', 'batch')
DIRECTIONS = ('maximize', 'minimize')
# Keys an evaluator entry carries. Anything else a client sends is dropped, so
# a saved workflow cannot accumulate settings nothing reads.
ENTRY_KEYS = ('name', 'description', 'code', 'config', 'metric', 'direction',
              'timing', 'timeout', 'labels', 'enabled')
MAX_EVALUATORS = 50

EXAMPLE_CODE = '''def build_evaluator(threshold: float = 0.5):
    """Typed parameters with defaults become the form Studio shows."""
    class Evaluator:
        def evaluate(self, records):
            done = [r for r in records if r["status"] == "success"]
            return {"metrics": {"completion_rate": len(done) / len(records)},
                    "coverage": {"unit": "records", "total": len(records),
                                 "scored": len(records), "unscored": 0}}
    return Evaluator()
'''


def entries(graph):
    """The workflow's evaluators, in order."""
    return [e for e in (graph.get('evaluators') or []) if isinstance(e, dict)]


def evaluator_name(value):
    """An evaluator name as Evolve sends it. Its objective is written
    `canvas:<name>`, from when an evaluator was a node on the canvas; a bare
    name means the same thing."""
    if isinstance(value, str) and value.startswith('canvas:'):
        return value[len('canvas:'):]
    return value


def find(graph, name):
    name = evaluator_name(name)
    return next((e for e in entries(graph) if e.get('name') == name), None)


def label_resource_ids(graph):
    """The uploaded data resources a workflow's evaluators read labels from."""
    found = set()
    for entry in entries(graph):
        labels = entry.get('labels')
        if isinstance(labels, str):
            found.add(labels)
        elif isinstance(labels, dict) and labels.get('resource_id'):
            found.add(labels['resource_id'])
    return found


def label_config(labels):
    """A labels setting as a DataLoader configuration. The frontend sends the
    resource id; an older graph stored the whole loader configuration."""
    return {'resource_id': labels, 'loader': 'auto'} if isinstance(labels, str) else dict(labels or {})


def timing_of(entry):
    return (entry or {}).get('timing') or DEFAULT_TIMING


def timeout_of(entry):
    return int((entry or {}).get('timeout') or DEFAULT_TIMEOUT)


def has_timing(graph, timing):
    return any(e.get('enabled', True) and timing_of(e) == timing for e in entries(graph))


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_entry(entry, draft=False, check_code=True):
    """What a save or a run refuses.

    A switched-off evaluator, or a new one with no code yet, is a draft: it
    must not stop the workflow from being saved (`draft`, and `check_code` for
    code that does not parse yet). It is checked in full when it is used (it
    then reports its own failure) or chosen as an Evolve objective.
    """
    if not isinstance(entry, dict):
        raise SourceError('Each evaluator must be an object.')
    name = entry.get('name')
    if not isinstance(name, str) or not name.strip():
        raise SourceError('Every evaluator needs a name.')
    if timing_of(entry) not in TIMINGS:
        raise SourceError(f"Evaluator '{name}' timing must be one of {', '.join(TIMINGS)}.")
    timeout = entry.get('timeout', DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 10 <= timeout <= 3600:
        raise SourceError('Evaluator time limit must be a whole number of seconds between 10 and 3600.')
    if entry.get('direction', 'maximize') not in DIRECTIONS:
        raise SourceError('Evaluator direction must be maximize or minimize.')
    if entry.get('config') is not None and not isinstance(entry['config'], dict):
        raise SourceError('Evaluator parameters must be an object of {parameter: value}.')
    if entry.get('metric') is not None and not isinstance(entry['metric'], str):
        raise SourceError('Evaluator metric must be the name of a metric the code returns.')
    if entry.get('labels') is not None and not isinstance(entry['labels'], (str, dict)):
        raise SourceError('Evaluator labels must be a data resource.')
    if entry.get('enabled') is not None and not isinstance(entry['enabled'], bool):
        raise SourceError('Evaluator enabled must be true or false.')
    code = entry.get('code')
    if code is not None and not isinstance(code, str):
        raise SourceError('Evaluator code must be Python source text.')
    if not (code or '').strip():
        if draft:
            return entry
        raise SourceError(f"Evaluator '{name}' has no code yet. Write its evaluation code in Evaluate & Evolve.")
    if not check_code:
        return entry
    from .python_evaluator import interface
    try:
        interface(code)
    except (ValueError, TypeError) as exc:
        raise SourceError(f"Evaluator '{name}': {exc}") from exc
    return entry


def normalize(entry):
    """One evaluator, with only the keys the platform reads."""
    kept = {k: entry[k] for k in ENTRY_KEYS if k in entry}
    kept['name'] = (kept.get('name') or '').strip()
    kept.setdefault('code', '')
    kept['config'] = kept.get('config') or {}
    kept.setdefault('metric', None)
    kept['direction'] = kept.get('direction') or 'maximize'
    kept['timing'] = timing_of(kept)
    kept['timeout'] = timeout_of(kept)
    kept.setdefault('labels', None)
    kept['enabled'] = kept.get('enabled', True)
    return kept


def validate_evaluators(evaluators, draft=True):
    """Validate and normalize the list a save or a PUT hands in."""
    if evaluators in (None, ''):
        return []
    if not isinstance(evaluators, list):
        raise SourceError('Evaluators must be a list.')
    if len(evaluators) > MAX_EVALUATORS:
        raise SourceError(f'A workflow keeps at most {MAX_EVALUATORS} evaluators.')
    result, seen = [], set()
    for entry in evaluators:
        # A switched-off evaluator is not checked in full: it is where an
        # unfinished one is parked, and parking it must always be possible.
        off = isinstance(entry, dict) and entry.get('enabled') is False
        validate_entry(entry, draft=draft or off, check_code=not off)
        kept = normalize(entry)
        if kept['name'] in seen:
            raise SourceError(f"Two evaluators are called '{kept['name']}'. Evaluator names must be unique.")
        seen.add(kept['name'])
        result.append(kept)
    return result


def validate_graph(graph):
    """What a save or a run refuses about a whole workflow's evaluators."""
    validate_evaluators(graph.get('evaluators'))


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

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
    """Pass complete saved executions to the code, without grouping or filtering.

    `cfg` is an evaluator entry (Python code) or, for the `evaluate_records`
    library tool, one of the legacy fixed scorers.
    """
    cfg, records = copy.deepcopy(cfg), copy.deepcopy(records)
    kind = cfg.get('type', 'python')
    if kind not in {e['id'] for e in CATALOG}:
        raise SourceError('Unknown evaluator type.')
    if kind == 'tool' and cfg.get('tool') in ('read_dataset', 'evaluate_records'):
        raise SourceError('Choose a non-recursive custom evaluator.')
    if kind in ('tool', 'python'):
        logs = ''
        if kind == 'python':
            from backend.features.chat import chat_control
            try:
                outcome = chat_control.worker('evaluate_python', {'code': cfg.get('code') or '', 'records': records,
                                                                  'config': cfg.get('config') or {}}, timeout=timeout_of(cfg))
            except TimeoutError as exc:
                raise SourceError(f'Evaluator took longer than its {timeout_of(cfg)}-second limit. Raise the limit in the evaluator settings or make the code faster.') from exc
            if isinstance(outcome, dict) and outcome.get('studio_evaluator_output'):
                result, logs = outcome['report'], outcome.get('logs') or ''
            else:
                result = outcome
        else:
            from backend.api import tools_registry
            result = tools_registry.call_tool(cfg['tool'], {'records': records, 'config': cfg.get('config') or {}})
        if not isinstance(result, dict) or not isinstance(result.get('metrics'), dict):
            raise SourceError('Evaluator code returns {metrics: {name: number}} with optional records, coverage and details.')
        result = copy.deepcopy(result)
        if logs:
            result['logs'] = logs
        # No objective chosen yet (never previewed, or the code changed): the
        # first metric the code returns, said so, rather than a failed report.
        metric = cfg.get('metric') or next(iter(result['metrics']), None)
        if metric is None:
            raise SourceError('Evaluator returned no metrics. Return {"metrics": {"name": number}}.')
        result['objective'] = {'metric': metric, 'direction': cfg.get('direction', 'maximize'),
                               **({} if cfg.get('metric') else {'chosen': 'first metric returned'})}
        if metric not in result['metrics']:
            raise SourceError(f'Evaluator did not return its objective metric {metric!r}; it returned {sorted(result["metrics"])}. Choose one of these in the evaluator settings.')
        if any(v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)) for v in result['metrics'].values()):
            raise SourceError('Evaluator metrics must be finite numbers or null.')
        if 'records' in result and (not isinstance(result['records'], list) or any(not isinstance(r, dict) for r in result['records'])):
            raise SourceError('Evaluator records must be a list of objects; their granularity is chosen by the code.')
        for row in result.get('records', []):
            value = row.get('score')
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
                raise SourceError('Evaluator scores must be finite numbers or null.')
        coverage = result.get('coverage')
        if coverage is not None:
            if not isinstance(coverage, dict) or not isinstance(coverage.get('unit'), str) or not coverage['unit'].strip():
                raise SourceError('Coverage must declare its unit.')
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
            'coverage': {'total': len(details), 'scored': len(scores), 'unscored': len(details) - len(scores)},
            'objective': {'metric': metric, 'direction': cfg.get('direction', 'maximize')}}


def record_for(run, record_id=None):
    """One saved execution, as the evaluation code receives it.

    Every saved field is kept, including errors, provenance and the execution
    snapshot: evaluation is no longer wired into the canvas, so nothing here
    filters what the code may look at.
    """
    values = {n['name']: n.get('output') for n in run.get('nodes', [])}
    values.update(run.get('node_outputs') or {})
    inputs = run.get('inputs') or run.get('_effective_inputs') or {}
    record = copy.deepcopy({k: v for k, v in run.items() if not k.startswith('_')})
    record.update({'id': record_id or run.get('id') or run.get('run_id', 'record'),
                   'inputs': copy.deepcopy(inputs), 'node_outputs': copy.deepcopy(values),
                   'prediction': run.get('result', run.get('prediction')),
                   'status': run.get('status', 'success')})
    record.setdefault('expected', None)
    return record


def evaluate_entries(evaluators, runs, names=None, timing=None):
    """Reports per evaluator. Never raises: an evaluator that is misconfigured
    reports its own failure, and the runs it scores keep their status."""
    reports = {}
    for entry in evaluators or []:
        name = entry.get('name')
        if not name or (names is not None and name not in names):
            continue
        if names is None and not entry.get('enabled', True):
            continue
        if timing and timing_of(entry) not in timing:
            continue
        # The report keeps the settings, never the label records themselves:
        # they are copied into every run and batch that is saved, and into
        # what Evolve shows its prompt proposer.
        shown = public_config(entry)
        try:
            validate_entry(entry)
            cfg = {**copy.deepcopy(entry), 'type': 'python'}
            records = [record_for(run, str(i)) for i, run in enumerate(runs)]
            # Labels are a separate resource and are never injected into Agents.
            # The code owns label matching; no platform-imposed join or grouping.
            if cfg.get('labels') or '_label_records' in cfg:
                from backend.features.data.dataloaders import records as load_records
                labels = cfg['_label_records'] if '_label_records' in cfg else load_records(label_config(cfg['labels']))
                cfg['config'] = {**(cfg.get('config') or {}), 'label_records': copy.deepcopy(labels)}
            reports[name] = {'status': 'success', **report(cfg, records), 'config': shown}
        except Exception as exc:
            reports[name] = {'status': 'failed', 'error': str(exc), 'config': shown}
    return reports


def evaluate_runs(graph, runs, names=None, timing=None):
    return evaluate_entries(entries(graph), runs, names, timing)


def public_config(cfg):
    """An evaluator's settings without label data frozen or injected into it."""
    shown = copy.deepcopy(cfg or {})
    if '_label_records' in shown:
        shown.pop('_label_records')
        shown['labels'] = shown.get('labels') or 'frozen for this evaluation'
    if isinstance(shown.get('config'), dict):
        shown['config'].pop('label_records', None)
    return shown


# ---------------------------------------------------------------------------
# Interface discovery
# ---------------------------------------------------------------------------

def described(code):
    """The form the code declares: which entrypoint, and its typed parameters.

    Never raises: a syntax error or an undiscoverable signature is part of the
    answer, because the panel shows it beside the editor while it is typed.
    """
    from .python_evaluator import interface
    try:
        schema = interface(code or '')
    except (ValueError, TypeError) as exc:
        return {'kind': None, 'params': [], 'provided': [], 'warnings': [], 'error': str(exc)}
    return {'kind': 'factory' if schema['entrypoint'] == 'build_evaluator' else 'function',
            'params': [{'name': f['name'], 'type': f['type'], 'required': f['required'],
                        'default': f.get('default'), 'description': f.get('description', '')}
                       for f in schema['inputs']],
            'provided': schema.get('provided_inputs', []),
            'warnings': schema.get('warnings', []),
            'code_hash': schema.get('code_hash'),
            'error': None}


# ---------------------------------------------------------------------------
# Drafts: pasted code kept while the user goes elsewhere for a run id
# ---------------------------------------------------------------------------

def _draft_path(graph_id):
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,200}', graph_id or '') or graph_id.startswith('.'):
        raise SourceError('Unknown workflow.')
    return DRAFTS_DIR / f'{graph_id}.json'


def get_draft(graph_id):
    path = _draft_path(graph_id)
    if not path.is_file():
        return None
    try:
        with open(path, encoding='utf-8') as stream:
            draft = json.load(stream)
    except (OSError, json.JSONDecodeError):
        return None
    return draft if isinstance(draft, dict) else None


def put_draft(graph_id, code, config=None, name=None):
    """Keep a pasted evaluator as it is. Drafts are never validated and never
    run; they only have to survive leaving the panel, and a restart."""
    from datetime import datetime, timezone
    if not isinstance(code, str):
        raise SourceError('A draft keeps the code you pasted.')
    if config is not None and not isinstance(config, dict):
        raise SourceError('Draft parameters must be an object of {parameter: value}.')
    draft = {'code': code, 'config': config or {},
             'updated_at': datetime.now(timezone.utc).isoformat()}
    if isinstance(name, str) and name.strip():
        draft['name'] = name.strip()
    path = _draft_path(graph_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(draft, stream, ensure_ascii=False, indent=2)
    temporary.replace(path)
    return draft


def clear_draft(graph_id, code=None):
    """Drop the draft. With `code`, only when that same code is now saved."""
    draft = get_draft(graph_id)
    if draft is None:
        return False
    if code is not None and (draft.get('code') or '').strip() != (code or '').strip():
        return False
    _draft_path(graph_id).unlink(missing_ok=True)
    return True


def clear_saved_drafts(graph_id, evaluators):
    """After a save: the draft is gone once its code is one of the evaluators."""
    draft = get_draft(graph_id)
    if draft is None:
        return
    pasted = (draft.get('code') or '').strip()
    if pasted and any((e.get('code') or '').strip() == pasted for e in evaluators or []):
        clear_draft(graph_id)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _graph(graph_id):
    from backend.api import graphs
    graph = graphs.load_graph(graph_id)
    if not graph:
        raise HTTPException(404, 'Workflow not found.')
    return graph


def saved_runs(graph_id, body):
    from backend.api import runner, batch
    if body.get('batch_id'):
        saved = batch.get_batch(body['batch_id'])
        if not saved or saved.get('graph_id') != graph_id:
            raise HTTPException(404, 'Batch not found in this workflow.')
        if saved.get('status') in ('running', 'pending', 'queued', 'cancelling'):
            raise HTTPException(409, 'Wait for the batch to finish or stop it first.')
        return [(runner.get_run(i.get('run_id')) if i.get('run_id') else None) or {**i, 'nodes': []} for i in saved['items']]
    run = runner.get_run(body.get('run_id', ''))
    if not run or run.get('graph_id') != graph_id:
        raise HTTPException(404, 'Run not found in this workflow.')
    if run.get('status') in ('running', 'pending', 'queued', 'cancelling'):
        raise HTTPException(409, 'Wait for execution to finish.')
    return [run]


@router.get('/evaluators')
def catalog():
    """What an evaluator is, for a panel that has none yet: the entrypoints
    the code may define, the timings, the defaults and an example."""
    return {'entrypoints': [
                {'kind': 'factory', 'name': 'build_evaluator',
                 'description': 'build_evaluator(**typed parameters) returns an object with evaluate(records).'},
                {'kind': 'function', 'name': 'evaluate',
                 'description': 'evaluate(records, **typed parameters) returns the report directly.'}],
            'timings': [{'id': 'manual', 'label': 'Only when I ask'},
                        {'id': 'run', 'label': 'After each run'},
                        {'id': 'batch', 'label': 'After the batch'}],
            'defaults': {'timing': DEFAULT_TIMING, 'timeout': DEFAULT_TIMEOUT, 'direction': 'maximize'},
            'example_code': EXAMPLE_CODE}


@router.post('/evaluators/interface')
def inspect_code(body: dict = Body(...)):
    return described(body.get('code') or '')


@router.get('/graphs/{graph_id}/evaluators')
def list_evaluators(graph_id: str):
    return {'evaluators': entries(_graph(graph_id))}


@router.put('/graphs/{graph_id}/evaluators')
def replace_evaluators(graph_id: str, body=Body(...)):
    from backend.api import graphs
    _graph(graph_id)
    payload = body.get('evaluators') if isinstance(body, dict) else body
    try:
        saved = graphs.set_evaluators(graph_id, payload)
    except (SourceError, graphs.GraphValidationError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {'evaluators': entries(saved)}


@router.get('/graphs/{graph_id}/evaluators/draft')
def read_draft(graph_id: str):
    _graph(graph_id)
    return {'draft': get_draft(graph_id)}


@router.put('/graphs/{graph_id}/evaluators/draft')
def write_draft(graph_id: str, body: dict = Body(...)):
    _graph(graph_id)
    try:
        return {'draft': put_draft(graph_id, body.get('code') or '', body.get('config'), body.get('name'))}
    except SourceError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.delete('/graphs/{graph_id}/evaluators/draft')
def delete_draft(graph_id: str):
    _graph(graph_id)
    return {'cleared': clear_draft(graph_id)}


@router.post('/graphs/{graph_id}/evaluators/preview')
def preview_code(graph_id: str, body: dict = Body(...)):
    """Run code over saved results without saving anything."""
    _graph(graph_id)
    entry = _ad_hoc(body)
    if entry is None:
        raise HTTPException(422, 'Paste the evaluation code to preview.')
    runs = saved_runs(graph_id, body)
    result = evaluate_entries([entry], runs, [entry['name']]).get(entry['name'], {})
    if result.get('status') != 'success':
        raise HTTPException(422, result.get('error') or 'Evaluator failed.')
    result.pop('config', None)
    return {'report': result, 'execution_count': len(runs),
            'metrics': [{'name': key, 'type': 'number', 'nullable': value is None, 'sample': value}
                        for key, value in result['metrics'].items()]}


@router.post('/graphs/{graph_id}/evaluators/run')
def run_evaluators(graph_id: str, body: dict = Body(...)):
    """Evaluate saved results and keep the report with that batch or run."""
    graph = _graph(graph_id)
    runs = saved_runs(graph_id, body)
    entry = _ad_hoc(body)
    if entry is not None:
        evaluations = evaluate_entries([entry], runs, [entry['name']])
    else:
        names = body.get('names')
        if body.get('name'):
            names = [body['name']]
        if names is not None:
            if not isinstance(names, list) or any(not isinstance(n, str) for n in names):
                raise HTTPException(422, 'Name the evaluators to run.')
            missing = [n for n in names if find(graph, n) is None]
            if missing:
                raise HTTPException(404, f'This workflow has no evaluator called {missing[0]!r}.')
            names = [evaluator_name(n) for n in names]
        evaluations = evaluate_runs(graph, runs, names)
    if body.get('batch_id'):
        from backend.api import batch
        batch.set_evaluations(body['batch_id'], evaluations)
    else:
        from backend.api import runner
        runner.set_evaluations(body.get('run_id', ''), evaluations)
    return {'evaluations': evaluations}


@router.delete('/graphs/{graph_id}/evaluators/{name}')
def delete_evaluator(graph_id: str, name: str):
    from backend.api import graphs
    graph = _graph(graph_id)
    if find(graph, name) is None:
        raise HTTPException(404, f'This workflow has no evaluator called {name!r}.')
    kept = [e for e in entries(graph) if e.get('name') != name]
    try:
        saved = graphs.set_evaluators(graph_id, kept)
    except (SourceError, graphs.GraphValidationError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {'evaluators': entries(saved)}


def _ad_hoc(body):
    """The unsaved evaluator a preview or a run was handed, if it was."""
    if not (body.get('code') or '').strip():
        return None
    entry = normalize({'name': body.get('name') or 'preview', 'code': body['code'],
                       'config': body.get('config') or {}, 'metric': body.get('metric'),
                       'direction': body.get('direction') or 'maximize',
                       'timing': 'manual', 'timeout': body.get('timeout') or DEFAULT_TIMEOUT,
                       'labels': body.get('labels'), 'enabled': True})
    try:
        validate_entry(entry)
    except SourceError as exc:
        raise HTTPException(422, str(exc)) from exc
    return entry
