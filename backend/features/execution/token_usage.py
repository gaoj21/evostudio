"""Token accounting, from what the `llm` package reports for each call.

Usage is recorded the moment a model call returns, into the run state that
the live endpoints read, so a run in progress shows what it has spent so far.
Totals are replaced rather than mutated in place: a reader serialising the
state mid-run sees one consistent snapshot, never half an update.
"""
import threading

_lock = threading.Lock()


def reported_usage(value):
    if not isinstance(value, dict):
        value = value.model_dump() if hasattr(value, 'model_dump') else vars(value) if hasattr(value, '__dict__') else {}
    inp, out = value.get('input_tokens', value.get('prompt_tokens')), value.get('output_tokens', value.get('completion_tokens'))
    if any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in (inp, out)):
        return None
    total = value.get('total_tokens')
    return {'input_tokens':inp, 'output_tokens':out, 'total_tokens':total if isinstance(total,int) and total >= inp + out else inp + out}


def add_usage(target, usage):
    for key in ('input_tokens','output_tokens','total_tokens'):
        target[key] = target.get(key, 0) + usage[key]
    target['reported_calls'] = target.get('reported_calls', 0) + usage.get('reported_calls', 1)
    target['source'] = 'provider'


def combined(*usages):
    """The sum of several usage records; None when none of them reported any."""
    total = {}
    for usage in usages:
        if usage and usage.get('reported_calls'):
            add_usage(total, usage)
    return total or None


def record(state, usage):
    """Add one reported call to the run, and to the node running it if any."""
    if not usage:
        return
    with _lock:
        for target in (state, state.get('_usage_node')):
            if isinstance(target, dict):
                total = dict(target.get('token_usage') or {})
                add_usage(total, usage)
                target['token_usage'] = total


def usage_key(state) -> str:
    """The key of this state's usage hook, registering it the first time.

    A model built with the key — and every agent the framework clones from
    its config — reports each call's `LLMResult.usage` here, so the run, its
    node and its batch show what has been spent while the work is still
    running. Nothing reads a provider response: the package already told us.
    """
    from backend.features import model_bridge
    key = state.get('_usage_key')
    if key:
        return key
    key = model_bridge.usage_hook(lambda result: record(state, reported_usage(result.usage)))
    state['_usage_key'] = key
    return key


def release_usage(state) -> None:
    """Drop the hook when the run, batch or task settles."""
    from backend.features import model_bridge
    model_bridge.release_usage_hook(state.pop('_usage_key', None))


def attach_usage(model, key):
    """Make a model the framework built for us report to `key` as well.

    `add_agents_from_workflow` builds each agent's model from the config it is
    handed, which already carries the key; this covers a model built from a
    config that lost it (a rebuild that passed none) without touching any
    provider or framework model class.
    """
    config = getattr(model, 'config', None)
    if not key or config is None or not hasattr(config, 'usage_key'):
        return model
    for target, attribute in ((config, 'usage_key'), (model, 'usage_key')):
        try:
            object.__setattr__(target, attribute, key)
        except (AttributeError, TypeError):
            try:
                setattr(target, attribute, key)
            except Exception:
                pass
    return model
