"""Schedule dispatch uses the same Dataset batch engine as the Run dialog."""
from backend.features.data import input_composition
from backend.api import runner, batch, sources


def loader_node(graph):
    nodes = input_composition.connected(graph)
    if not any((n.get('source') or {}).get('type') == 'dataloader'
               and not input_composition.is_reference(n) for n in nodes):
        return None
    try:
        node = input_composition.primary(graph)
    except sources.SourceError as exc:
        raise ValueError(str(exc)) from exc
    if node['source'].get('loader') != 'python':
        raise ValueError('Scheduled DataLoader execution requires a Python Dataset.')
    return node


def execution_kind(graph):
    return 'batch' if loader_node(graph) else 'run'


def get_execution(occurrence):
    if occurrence.get('kind') == 'batch':
        return batch.get_batch(occurrence['run_id'])
    return runner.get_run(occurrence['run_id'])


def launch(graph, inputs, schedule, occurrence):
    execution_id, due = occurrence['run_id'], occurrence['due']
    if occurrence.get('kind') == 'batch':
        previous = batch.get_batch(execution_id)
        if previous:
            if previous['status'] == 'succeeded':
                return execution_id
            outcome = batch.resume_batch(execution_id, graph)
            if not outcome.get('resumed'):
                raise ValueError(outcome.get('reason', 'Could not resume scheduled batch.'))
            return execution_id
        from .loader_run import start
        context = {'scheduled_at': due, 'experiment_id': schedule['experiment_id'],
                   'session': schedule['session']}
        return start(graph, loader_node(graph), {'inputs': inputs, 'workers': 1}, batch_id=execution_id,
                     session=schedule['session'], session_started_at=due,
                     run_context=context)['batch_id']
    return runner.start_run(graph, inputs, background=True, session=schedule['session'],
                            run_id=execution_id, session_started_at=due)
