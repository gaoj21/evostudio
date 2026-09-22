"""Provider-reported token accounting, without changing model adapters."""
import threading


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


def attach_model(model, state):
    """Observe an existing response hook when available; never infer zero usage."""
    original = getattr(model, '_update_cost', None)
    if not callable(original): return
    if getattr(model, '_studio_usage_state', None) is state: return
    object.__setattr__(model, '_studio_usage_state', state)
    lock = threading.Lock()
    def tracked(response, *args, **kwargs):
        raw = response.get('usage') if isinstance(response,dict) else getattr(response,'usage',None)
        usage = reported_usage(raw)
        if usage:
            with lock: add_usage(state.setdefault('token_usage', {}), usage)
        return original(response, *args, **kwargs)
    object.__setattr__(model, '_update_cost', tracked)
