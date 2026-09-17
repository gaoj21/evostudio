"""Run the actual canvas and use an attached evaluator as the objective.

Candidates use identical prepared inputs and fresh, isolated workflow memory.
No provider implementation is embedded here. Saved-result proposals remain a
separate no-replay operation.
"""
import copy
from backend.api.sources import SourceError
from . import evaluator_tools


def select(graph, name):
    evaluator_tools.validate_graph(graph)
    task = next((t for t in graph.get('tasks', []) if t.get('name') == name and t.get('kind') == 'evaluator' and t.get('enabled', True)), None)
    if task is None:
        raise SourceError('Choose an enabled canvas evaluator.')
    return task


def score_saved(graph, records, name):
    select(graph, name)
    runs = copy.deepcopy(records)
    result = evaluator_tools.evaluate_runs(graph, runs, [name])[name]
    if result['status'] != 'success':
        raise SourceError(result['error'])
    metric = result['objective']['metric']
    return {'metrics': {'score': result['metrics'].get(metric), **(result.get('coverage') or {})},
            'records': {str(r.get('id', i)): {'metrics': {'score': r.get('score')}, 'reason': r.get('reason')} for i, r in enumerate(result.get('records', []))},
            'evaluations': {name: result}, 'objective': result['objective']}


def prepare(graph, body):
    from backend.features.data import input_composition as composition
    from backend.api import sources
    select(graph, body.get('evaluator'))
    mode = body.get('mode', 'evaluate')
    if mode not in ('evaluate', 'evolve_evaluate'):
        raise SourceError('Choose evaluation or evolution with evaluation.')
    if any((t.get('memory') or {}).get('provider') == 'mem0' and t.get('use_long_term_memory') for t in graph.get('tasks', [])):
        raise SourceError('Canvas candidate replay requires isolated memory. Use table/local memory or evaluate saved results for shared Mem0 workflows.')
    available = [t['name'] for t in graph['tasks'] if t.get('kind') not in ('source', 'tool', 'evaluator')]
    chosen = [] if mode == 'evaluate' else body.get('nodes', available)
    if not isinstance(chosen, list) or any(n not in available for n in chosen) or (mode != 'evaluate' and not chosen):
        raise SourceError('Choose valid prompt nodes to optimize.')
    rounds = body.get('rounds', 1)
    if not isinstance(rounds, int) or isinstance(rounds, bool) or not 1 <= rounds <= 10:
        raise SourceError('Choose 1–10 candidate rounds.')
    main = composition.primary(graph)
    rows = composition.load_primary(graph, main)
    if not rows:
        raise SourceError('DataLoader produced no records.')
    from backend.api import preprocess
    rows = preprocess.apply(graph, rows)
    return rows, {'mode': mode, 'nodes': chosen, 'evaluator': body['evaluator'], 'rounds': rounds,
                  'source': {'type': 'canvas', 'node': main['name'], 'config': copy.deepcopy(main['source'])},
                  'n_dev': len(rows), 'n_train': 0, 'memory_start': 'empty isolated stores'}


def execute(state, graph, rows, params, stage):
    from backend.api import runner
    from . import saved_result_evolution as saved
    # Freeze external labels once, just like prepared workflow inputs.
    graph = copy.deepcopy(graph)
    for task in graph['tasks']:
        cfg = task.get('evaluator') or {}
        if cfg.get('labels'):
            from backend.features.data.dataloaders import records
            cfg['_label_records'] = records(cfg.pop('labels'))
    name = params['evaluator']
    def run(candidate, generation):
        candidate = copy.deepcopy(candidate)
        candidate['id'] = f"evolve-{state['task_id']}-{generation}"
        candidate['preprocess'] = None  # already prepared, once for the experiment
        records = []
        blocked = set()
        from backend.features.execution.batch import _group_key
        for i, row in enumerate(rows):
            stage(f'{generation}: record {i+1}/{len(rows)}')
            group = _group_key(row)
            if group is not None and group in blocked:
                id, output = None, {'status':'blocked', 'error':'An earlier observation in this trajectory failed.'}
            else:
                id = runner.start_run(candidate, copy.deepcopy(row), background=False)
                output = runner.get_run(id) or {}
            if group is not None and output.get('status') != 'success':
                blocked.add(group)
            records.append({**copy.deepcopy(output), 'id': str(i), 'run_id': id, 'inputs': row, 'status': output.get('status', 'failed'),
                            'error': output.get('error'), 'prediction': output.get('result'),
                            'nodes': output.get('nodes', []), 'node_outputs': output.get('node_outputs', {})})
        return records, score_saved(graph, records, name)
    best_graph = copy.deepcopy(graph)
    evidence, baseline = run(best_graph, 'baseline')
    state['baseline'] = baseline
    if params['mode'] == 'evaluate':
        return
    if baseline['metrics']['score'] is None:
        raise SourceError('Evaluator has no usable objective score. Supply labels or fix the evaluator before optimizing.')
    best = baseline
    direction = best['objective']['direction']
    state['candidates'] = []
    for round_no in range(params['rounds']):
        stage(f'proposing candidate {round_no+1}')
        candidate, _, _ = saved.propose(best_graph, evidence, params['nodes'], runner._make_llm(), feedback=best)
        traces, result = run(candidate, f'candidate-{round_no+1}')
        value = result['metrics']['score']
        old = best['metrics']['score']
        comparable = result['evaluations'][name].get('coverage') == best['evaluations'][name].get('coverage')
        # Do not reward a candidate that succeeds on fewer records.
        comparable = comparable and sum(r['status'] == 'success' for r in traces) >= sum(r['status'] == 'success' for r in evidence)
        better = value is not None and comparable and (value > old if direction == 'maximize' else value < old)
        state['candidates'].append({'round': round_no+1, 'metrics': result['metrics'], 'accepted': better})
        if better:
            best_graph, best, evidence = candidate, result, traces
    # Internal frozen label data must not become persistent graph configuration.
    best_graph['id'] = graph['id']
    original_tasks = {t['name']: t for t in state['execution_graph']['tasks']}
    for task in best_graph['tasks']:
        if task.get('kind') == 'evaluator':
            task['evaluator'] = copy.deepcopy(original_tasks[task['name']].get('evaluator', {}))
    state['optimized'] = best
    state['optimized_graph'] = best_graph
    state['validation_status'] = 'evaluated'
    state['diff'] = [{'name': t['name'], 'before': original_tasks[t['name']].get('prompt', ''), 'after': t.get('prompt', ''),
                      'changed': original_tasks[t['name']].get('prompt', '') != t.get('prompt', '')}
                     for t in best_graph['tasks'] if t['name'] in params['nodes']]
