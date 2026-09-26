"""Run the actual canvas and use one of the workflow's evaluators as the objective.

Candidates use identical prepared inputs and fresh, isolated workflow memory.
No provider implementation is embedded here. Saved-result proposals remain a
separate no-replay operation.
"""
import copy
import hashlib
import shutil
from backend.api.sources import SourceError
from . import evaluator_tools

# Evolution holds out part of the data: prompts are proposed and chosen on
# dev, and val is scored once, at the end, for the baseline and the chosen
# candidate. It never decides anything, so its score is an honest estimate.
VAL_FRACTION = 0.3
# How val is chosen among the values of the split: by a seeded hash (any
# field — an entity), or the latest ones (an ordered field — a date).
SPLIT_ORDERS = ('random', 'latest')
# Records of one chunk a candidate replay runs at once.
REPLAY_WORKERS = 4

# A candidate replay runs under its own workflow id so that memory, tables,
# the session log and the run artifacts it writes are its own and empty. No
# real workflow id can start with an underscore (they are slugs of
# [a-z0-9-]), so everything under this prefix is a candidate's scratch space
# and safe to remove.
CANDIDATE_PREFIX = '_evolve-'


def candidate_id(task_id, generation):
    return f'{CANDIDATE_PREFIX}{task_id}-{generation}'


def discard_candidate_batches(batch_ids):
    """Remove the batches candidate replays ran, and their runs: their
    records are already in the task, and they belong to no real workflow."""
    from backend.api import batch, runner
    for batch_id in batch_ids or []:
        saved = batch.get_batch(batch_id) or {}
        for item in saved.get('items') or []:
            run_id = item.get('run_id')
            if run_id:
                with runner._lock:
                    runner._runs.pop(run_id, None)
                (runner.RUNS_DIR / f'{run_id}.json').unlink(missing_ok=True)
        with batch._lock:
            batch._batches.pop(batch_id, None)
            batch._threads.pop(batch_id, None)
        (batch.BATCHES_DIR / f'{batch_id}.json').unlink(missing_ok=True)


def split_units(graph, rows, field=None):
    """What dev and val are made of: one unit per entity, never per record —
    or, when the user chose a field of the Input's records, one per value of
    that field (a company, a date: whatever the data has).

    A record's output depends on everything its entity's earlier records
    wrote to memory, so an entity's whole timeline goes to one side. The
    entity is the trajectory the Input declares, else the key its memory is
    kept under; a record with neither stands alone. Returns (unit per row,
    what the unit is).
    """
    if field:
        from backend.features.data.dataloaders import get_path
        units, missing = [], 0
        for row in rows:
            try:
                value = get_path(row, field)
            except (KeyError, IndexError, TypeError):
                value = None
            if value is None or value == '':
                missing += 1
            units.append(value)
        if missing:
            raise SourceError(f"{missing} of {len(rows)} records have no '{field}'. "
                              'Split on a field every record of the Input has.')
        return units, field
    from backend.features.execution.batch import assign_sequences
    items = [{} for _ in rows]
    assign_sequences(graph, list(zip(items, rows)))
    units, kinds = [], set()
    for index, item in enumerate(items):
        if item.get('trajectory') is not None:
            units.append('t:' + str(item['trajectory'])); kinds.add('trajectory')
        elif item.get('memory_key') is not None:
            units.append('m:' + str(item['memory_key'])); kinds.add('memory entity')
        else:
            units.append(f'r:{index}'); kinds.add('record')
    return units, ' / '.join(sorted(kinds)) or 'record'


def _in_order(value):
    """Numbers by value, everything else by its text (ISO dates sort so)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (0, value, '')
    return (1, 0, str(value))


def held_out(units, fraction=VAL_FRACTION, seed=0, order='random'):
    """The val units: a fixed share of the distinct units — chosen by a hash
    of each unit and the seed (the same split every time, whatever order the
    data arrives in), or the latest ones when `order` is 'latest'. At least
    one unit on each side when there are two."""
    distinct = sorted(set(units), key=_in_order)
    if len(distinct) < 2:
        return set()
    size = min(len(distinct) - 1, max(1, round(len(distinct) * fraction)))
    if order == 'latest':
        return set(distinct[-size:])
    ranked = sorted(distinct, key=lambda u: hashlib.sha256(f'{seed}:{u}'.encode()).hexdigest())
    return set(ranked[:size])


def _store_roots():
    """Where the stores a replay can write to keep one workflow's data."""
    from backend.api import memory_store, table_store, workspace
    from backend.features.memory import stm_store
    return [(memory_store.MEMORY_DIR, ''), (table_store.TABLES_DIR, ''),
            (stm_store.STM_DIR, '.json'), (workspace.WORKSPACE_DIR, '')]


def discard_candidate_stores(task_id=None, keep=()):
    """Remove what candidate replays left behind: one task's scratch stores,
    or (with no task) every task's, apart from the ones still running.

    Only paths under CANDIDATE_PREFIX are touched, so a real workflow's
    memory, tables, session log and workspace can never be removed here."""
    prefix = f'{CANDIDATE_PREFIX}{task_id}-' if task_id else CANDIDATE_PREFIX
    spared = tuple(f'{CANDIDATE_PREFIX}{t}-' for t in keep)
    removed = []
    for root, suffix in _store_roots():
        if not root.is_dir():
            continue
        for path in root.iterdir():
            name = path.name[:-len(suffix)] if suffix and path.name.endswith(suffix) else path.name
            if not name.startswith(prefix) or any(name.startswith(s) for s in spared):
                continue
            try:
                shutil.rmtree(path) if path.is_dir() else path.unlink()
            except OSError:
                continue
            removed.append(str(path))
    return removed


def select(graph, name, objective=True):
    """The workflow evaluator a task scores with, by name. `objective`: it
    has to name the metric to optimize (Evolve), not merely score (Evaluate)."""
    name = evaluator_tools.evaluator_name(name)
    evaluator_tools.validate_graph(graph)
    entry = next((e for e in evaluator_tools.entries(graph)
                  if e.get('name') == name and e.get('enabled', True)), None)
    if entry is None:
        raise SourceError('Choose an enabled evaluator of this workflow.')
    # Saving tolerates a draft; an objective has to be complete.
    evaluator_tools.validate_entry(entry)
    # A run may fall back to the first metric the code returns; an
    # optimization objective must be the one the user chose.
    if objective and not entry.get('metric'):
        raise SourceError(f"Choose the objective metric of evaluator '{name}' first: preview it on saved results and pick the metric Evolve should optimize.")
    return entry


def score_saved(graph, records, name):
    name = evaluator_tools.evaluator_name(name)
    select(graph, name, objective=False)
    runs = copy.deepcopy(records)
    result = evaluator_tools.evaluate_runs(graph, runs, [name])[name]
    if result['status'] != 'success':
        raise SourceError(result['error'])
    metric = result['objective']['metric']
    return {'metrics': {'score': result['metrics'].get(metric), **(result.get('coverage') or {})},
            'records': {str(r.get('id', i)): {'metrics': {'score': r.get('score')}, 'reason': r.get('reason')} for i, r in enumerate(result.get('records', []))},
            'evaluations': {name: result}, 'objective': result['objective']}


def _shared_memory(graph):
    """A node that would reach a Mem0 store during a replay: (node, what it
    does, whose store). Reading is enough — an isolated candidate cannot
    start empty if it recalls another workflow's memories."""
    from backend.api import memory_policy
    by_name = {t.get('name'): t for t in graph.get('tasks', [])}
    for task in graph.get('tasks', []):
        if not task.get('use_long_term_memory'):
            continue
        name = task.get('name')
        if (task.get('memory') or {}).get('provider') == 'mem0':
            return (name, 'writes' if memory_policy.policy(task)['write_enabled'] else 'reads', name)
        read = memory_policy.stores_read_by(task)
        for other in (read if read is not None else []):
            if ((by_name.get(other) or {}).get('memory') or {}).get('provider') == 'mem0':
                return (name, 'reads', other)
    return None


def prepare(graph, body):
    """The settings, checked, and the Input's records. Reading the Input can
    take long: the route uses `settings` and the task reads it (`load_rows`)."""
    params = settings(graph, body)
    rows = load_rows(graph)
    return rows, {**params, 'n_dev': len(rows)}


def load_rows(graph):
    """Every record the canvas Input produces, preprocessed once for the
    whole experiment."""
    from backend.features.data import input_composition as composition
    from backend.api import preprocess
    rows = composition.load_primary(graph, composition.primary(graph))
    if not rows:
        raise SourceError('DataLoader produced no records.')
    return preprocess.apply(graph, rows)


def settings(graph, body):
    """What the experiment is, checked — without reading any data."""
    from backend.features.data import input_composition as composition
    mode = body.get('mode', 'evaluate')
    select(graph, body.get('evaluator'), objective=mode != 'evaluate')
    if mode not in ('evaluate', 'evolve_evaluate'):
        raise SourceError('Choose evaluation or evolution with evaluation.')
    shared = _shared_memory(graph)
    if shared:
        raise SourceError(
            f"Canvas candidate replay requires isolated memory, and '{shared[0]}' {shared[1]} shared Mem0 memory"
            f"{'' if shared[2] == shared[0] else f' of {shared[2]!r}'}. Candidates would read and write the same store, "
            'so their scores would not be comparable. Switch those nodes to table or local memory, or evaluate saved results instead.')
    available = [t['name'] for t in graph['tasks'] if t.get('kind') not in ('source', 'tool')]
    chosen = [] if mode == 'evaluate' else body.get('nodes', available)
    if not isinstance(chosen, list) or any(n not in available for n in chosen) or (mode != 'evaluate' and not chosen):
        raise SourceError('Choose valid prompt nodes to optimize.')
    rounds = body.get('rounds', 1)
    if not isinstance(rounds, int) or isinstance(rounds, bool) or not 1 <= rounds <= 10:
        raise SourceError('Choose 1–10 candidate rounds.')
    main = composition.primary(graph)
    share_labels = body.get('share_labels', False)
    if not isinstance(share_labels, bool):
        raise SourceError('share_labels must be true or false.')
    fraction = body.get('val_fraction', VAL_FRACTION)
    if isinstance(fraction, bool) or not isinstance(fraction, (int, float)) or not 0.1 <= fraction <= 0.5:
        raise SourceError('Hold out between 10% and 50% for validation.')
    seed = body.get('split_seed', 0)
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise SourceError('The split seed must be a whole number.')
    field = body.get('split_field') or None
    if field is not None and (not isinstance(field, str) or not field.strip()):
        raise SourceError('Split on the name of a field of the Input records.')
    order = body.get('split_order') or 'random'
    if order not in SPLIT_ORDERS:
        raise SourceError('Hold out values at random or the latest ones.')
    if order == 'latest' and not field:
        raise SourceError('Holding out the latest values needs a field to order them by.')
    from backend.api import batch
    workers = body.get('workers', REPLAY_WORKERS)
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= batch.MAX_WORKERS:
        raise SourceError(f'Run 1–{batch.MAX_WORKERS} records at once.')
    return {'mode': mode, 'nodes': chosen, 'evaluator': evaluator_tools.evaluator_name(body['evaluator']), 'rounds': rounds, 'share_labels': share_labels,
                  'val_fraction': float(fraction), 'split_seed': seed, 'workers': workers,
                  'split_field': field.strip() if field else None, 'split_order': order,
                  'source': {'type': 'canvas', 'node': main['name'], 'config': copy.deepcopy(main['source'])},
                  'n_dev': None, 'n_train': 0, 'memory_start': 'empty isolated stores'}


def execute(state, graph, rows, params, stage):
    """Score the canvas, then try candidates. Everything a replay writes —
    memory, tables, session log, run artifacts, files — goes to the task's
    own scratch stores and is removed when it ends, however it ends."""
    try:
        _execute(state, graph, rows, params, stage)
    finally:
        discard_candidate_stores(state['task_id'])
        discard_candidate_batches(state.pop('_candidate_batches', []))


def _workspace_for(state, graph):
    """A copy of the workflow's files for the candidates to work on.

    A copy rather than the real folder: a prompt under optimization can call
    a tool that writes files, and neither the user's workspace nor the next
    candidate may be changed by what a candidate did."""
    from backend.api import workspace
    workspace_id = f"{CANDIDATE_PREFIX}{state['task_id']}-files"
    real = workspace.files_dir(graph.get('id') or 'graph')
    if real.is_dir():
        target = workspace.files_dir(workspace_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(real, target, dirs_exist_ok=True)
    return workspace_id


def _execute(state, graph, rows, params, stage):
    from backend.api import runner
    from backend.features.execution.token_usage import combined, usage_key
    from . import saved_result_evolution as saved
    # Freeze external labels once, just like prepared workflow inputs.
    graph = copy.deepcopy(graph)
    for entry in evaluator_tools.entries(graph):
        if entry.get('labels'):
            from backend.features.data.dataloaders import records
            entry['_label_records'] = records(evaluator_tools.label_config(entry.pop('labels')))
    name = evaluator_tools.evaluator_name(params['evaluator'])
    workspace_id = _workspace_for(state, graph)
    def run(candidate, generation):
        """Replay every record with `candidate`, the way a batch runs: node
        by node, in the Input's own chunks, so what one chunk writes to
        memory is there for the next, and the records of a chunk in parallel.
        Returns one record per row, in row order."""
        from backend.api import batch
        candidate = copy.deepcopy(candidate)
        # Its own id: empty memory, tables and session log per candidate. The
        # files it works on are the workflow's, copied once for this task.
        candidate['id'] = candidate_id(state['task_id'], generation)
        candidate['_workspace_id'] = workspace_id
        candidate['preprocess'] = None  # already prepared, once for the experiment
        # Scored once, below, over every record.
        candidate['evaluators'] = []
        stage(f'{generation}: starting')
        batch_id = batch.start_batch(candidate, copy.deepcopy(rows), copy.deepcopy(params.get('source') or {}),
                                     workers=params.get('workers', REPLAY_WORKERS), mode='node')
        # Known to Stop, the live usage and the cleanup from here on.
        state['current_batch'] = batch_id
        state.setdefault('_candidate_batches', []).append(batch_id)
        shown = None
        try:
            while not batch.wait_for(batch_id, timeout=1):
                if state.get('stop_requested'):
                    batch.cancel_batch(batch_id)
                    stage('stopping')          # raises: a stopped replay is not a result
                at = (batch.get_batch(batch_id) or {}).get('stage') or {}
                text = (f"{generation}: node {at.get('index')}/{at.get('of')} · {at.get('node')} · "
                        f"{at.get('done', 0)}/{at.get('total', '?')}") if at.get('node') else f'{generation}: running'
                if text != shown:
                    stage(text); shown = text
        except BaseException:
            batch.cancel_batch(batch_id)
            raise
        done = batch.get_batch(batch_id) or {}
        # One step: the live view reads either the running batch or its
        # settled usage, never both.
        state.update(current_batch=None, token_usage=combined(state.get('token_usage'), done.get('token_usage')))
        if state.get('stop_requested'):
            stage('stopping')
        by_index = {item.get('index'): item for item in done.get('items') or []}
        records = []
        for i, row in enumerate(rows):
            item = by_index.get(i) or {}
            id = item.get('run_id')
            output = (runner.get_run(id) if id else None) or {'status': item.get('status', 'failed'),
                                                              'error': item.get('error')}
            records.append({**copy.deepcopy(output), 'id': str(i), 'run_id': id, 'inputs': row,
                            'status': output.get('status', 'failed'), 'error': output.get('error'),
                            'prediction': output.get('result'), 'nodes': output.get('nodes', []),
                            'node_outputs': output.get('node_outputs', {})})
        return records
    best_graph = copy.deepcopy(graph)
    everything = run(best_graph, 'baseline')
    if params['mode'] == 'evaluate':
        state['baseline'] = score_saved(graph, everything, name)
        return
    # Dev and val, by entity. Every candidate replays every record — memory
    # may be read across entities — but only dev is seen, scored and chosen
    # on; val is scored once, at the end.
    order = params.get('split_order') or 'random'
    units, unit_kind = split_units(graph, rows, params.get('split_field'))
    val_units = held_out(units, params.get('val_fraction', VAL_FRACTION), params.get('split_seed', 0), order)
    on_val = [unit in val_units for unit in units]
    def part(records, val):
        return [r for r, v in zip(records, on_val) if v == val]
    split = {'unit': unit_kind, 'field': params.get('split_field'), 'order': order,
             'seed': params.get('split_seed', 0),
             'val_fraction': params.get('val_fraction', VAL_FRACTION),
             'dev_units': len(set(units) - val_units), 'val_units': len(val_units),
             'dev_records': on_val.count(False), 'val_records': on_val.count(True)}
    state['split'] = split
    baseline_all = everything
    evidence = part(everything, False)
    baseline = score_saved(graph, evidence, name)
    state['baseline'] = baseline
    if baseline['metrics']['score'] is None:
        raise SourceError('Evaluator has no usable objective score. Supply labels or fix the evaluator before optimizing.')
    best, best_all = baseline, baseline_all
    direction = best['objective']['direction']
    state['candidates'] = []
    # What would give the answers away: a label read from the inputs.
    # A field the evaluation code was pointed at is treated as a label: the
    # proposer sees the score, never the answer. Generic on purpose — the
    # parameter names are the user's, so every string parameter value that
    # names a record input is withheld.
    entry = evaluator_tools.find(graph, name) or {}
    hidden = sorted({v for v in (entry.get('config') or {}).values() if isinstance(v, str)})
    for round_no in range(params['rounds']):
        stage(f'proposing candidate {round_no+1}')
        llm = runner._make_llm(usage_key=usage_key(state))
        candidate, diff, _ = saved.propose(best_graph, evidence, params['nodes'], llm, feedback=best,
                                           share_labels=params.get('share_labels', False), hidden_inputs=hidden)
        replayed = run(candidate, f'candidate-{round_no+1}')
        traces = part(replayed, False)
        result = score_saved(graph, traces, name)
        value = result['metrics']['score']
        old = best['metrics']['score']
        comparable = result['evaluations'][name].get('coverage') == best['evaluations'][name].get('coverage')
        # Do not reward a candidate that succeeds on fewer records.
        comparable = comparable and sum(r['status'] == 'success' for r in traces) >= sum(r['status'] == 'success' for r in evidence)
        better = value is not None and comparable and (value > old if direction == 'maximize' else value < old)
        state['candidates'].append({'round': round_no+1, 'metrics': result['metrics'], 'accepted': better,
                                    'comparable': comparable, 'compared_with': old,
                                    'changed': [d['name'] for d in diff if d['changed']],
                                    'prompts': {d['name']: d['after'] for d in diff if d['changed']}})
        if better:
            best_graph, best, evidence, best_all = candidate, result, traces, replayed
    # Internal frozen label data must not become persistent graph configuration.
    best_graph['id'] = graph['id']
    best_graph.pop('_workspace_id', None)
    original_tasks = {t['name']: t for t in state['execution_graph']['tasks']}
    # The workflow keeps its own evaluators: the code this task scored with
    # belongs to the task.
    best_graph['evaluators'] = copy.deepcopy(graph['_workflow_evaluators'] if '_workflow_evaluators' in graph
                                             else state['execution_graph'].get('evaluators') or [])
    best_graph.pop('_workflow_evaluators', None)
    state['optimized'] = best
    state['optimized_graph'] = best_graph
    # The held-out answer: decided nothing, so it is the estimate to trust.
    if split['val_units']:
        stage('scoring the held-out records')
        before = score_saved(graph, part(baseline_all, True), name)
        after = before if best_all is baseline_all else score_saved(graph, part(best_all, True), name)
        b, a = before['metrics'].get('score'), after['metrics'].get('score')
        improved = None if a is None or b is None else (a > b if direction == 'maximize' else a < b)
        state['validation'] = {**split, 'metric': best['objective'].get('metric'), 'direction': direction,
                               'baseline': before['metrics'], 'optimized': after['metrics'],
                               'improved': improved, 'changed': best_all is not baseline_all}
    else:
        state['validation'] = {**split, 'note': 'Too few entities to hold any out: nothing was validated.'}
    state['validation_status'] = 'evaluated'
    state['diff'] = [{'name': t['name'], 'before': original_tasks[t['name']].get('prompt', ''), 'after': t.get('prompt', ''),
                      'changed': original_tasks[t['name']].get('prompt', '') != t.get('prompt', '')}
                     for t in best_graph['tasks'] if t['name'] in params['nodes']]
