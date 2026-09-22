"""Provider-reported token accounting, without changing model adapters.

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


def response_usage(response):
    raw = response.get('usage') if isinstance(response, dict) else getattr(response, 'usage', None)
    if raw is None and not isinstance(response, dict):
        raw = getattr(response, 'usage_metadata', None) or (getattr(response, 'response_metadata', None) or {}).get('token_usage')
    return reported_usage(raw) if raw is not None else None


def attach_model(model, state):
    """Observe an existing response hook when available; never infer zero usage."""
    original = getattr(model, '_update_cost', None)
    if not callable(original): return
    if getattr(model, '_studio_usage_state', None) is state: return
    object.__setattr__(model, '_studio_usage_state', state)
    original = getattr(model, '_studio_usage_original', None) or original
    object.__setattr__(model, '_studio_usage_original', original)
    def tracked(response, *args, **kwargs):
        record(state, response_usage(response))
        return original(response, *args, **kwargs)
    object.__setattr__(model, '_update_cost', tracked)
