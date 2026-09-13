"""Public handoff contract for company-owned LLM batching; no provider code."""
import inspect


def batch_size(value):
    if value is None or value == '':
        return None
    if isinstance(value, bool) or not str(value).isdigit() or not 1 <= int(value) <= 1024:
        raise ValueError('API batch size must be a whole number between 1 and 1024.')
    return int(value)


def require_factory(factory):
    params = inspect.signature(factory).parameters
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return
    if not {'batch_size', 'batch_id'} <= params.keys():
        raise ValueError('Native API batching is not connected yet. The company LLM factory must accept batch_size and batch_id. Use standard execution until that adapter is ready.')


def validate(graph, value):
    size = batch_size(value)
    if size is not None:
        from llm import get_evoagentx_llm
        require_factory(get_evoagentx_llm)
        if any((t.get('harness') or {}).get('engine') == 'deepagents' for t in graph.get('tasks', [])):
            from llm import get_agent_model
            require_factory(get_agent_model)
    return size


def options(state):
    size = batch_size(state.get('llm_batch_size'))
    return {'batch_size': size, 'batch_id': state.get('batch_id')} if size is not None else {}
