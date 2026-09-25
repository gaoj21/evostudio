"""Token accounting, from what the `llm` package reports for each call.

Usage is recorded the moment a model call returns, into the run state that
the live endpoints read, so a run in progress shows what it has spent so far.
Totals are replaced rather than mutated in place: a reader serialising the
state mid-run sees one consistent snapshot, never half an update.
"""
import threading

_lock = threading.Lock()


# Counted when the provider reports them; absent otherwise, never zero-filled.
DETAIL_KEYS = ('cache_read_tokens', 'reasoning_tokens')


def _detail(value: dict, flat: str, *nested: tuple[str, str]):
    """A detail count, flat (LLMUsage) or nested (provider payload shapes)."""
    found = _count(value.get(flat))
    if found is not None:
        return found
    for section, name in nested:
        block = _as_mapping(value.get(section))
        found = _count(block.get(name))
        if found is not None:
            return found
    return None


# The names an LLMUsage — or a provider payload — may carry its counts under.
_USAGE_FIELDS = ('input_tokens', 'output_tokens', 'total_tokens', 'prompt_tokens',
                 'completion_tokens', 'cache_read_tokens', 'reasoning_tokens',
                 'input_token_details', 'output_token_details',
                 'prompt_tokens_details', 'completion_tokens_details')


def _as_mapping(value) -> dict:
    """A usage object's counts as a dict, whatever class carries them.

    The contract fixes the field names of `LLMUsage`, not its class: a
    dataclass (with or without slots), a pydantic model, a plain object or a
    dict are all valid, so every one is read, the fields last of all.
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    for method in ('as_dict', 'to_dict', 'model_dump', 'dict'):
        convert = getattr(value, method, None)
        if callable(convert):
            try:
                found = convert()
            except Exception:
                continue
            if isinstance(found, dict):
                return found
    import dataclasses
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        try:
            return dataclasses.asdict(value)
        except Exception:
            pass
    found = {}
    for name in _USAGE_FIELDS:
        try:
            field = getattr(value, name)
        except Exception:
            continue
        if field is not None and not callable(field):
            found[name] = field
    return found


def _count(value):
    """A token count, or None: ints (and whole floats) of zero or more."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return value if isinstance(value, int) and value >= 0 else None


def reported_usage(value):
    """What a provider reported for one call, or None when it reported nothing."""
    value = _as_mapping(value)
    inp = _count(value.get('input_tokens', value.get('prompt_tokens')))
    out = _count(value.get('output_tokens', value.get('completion_tokens')))
    if inp is None or out is None:
        return None
    total = _count(value.get('total_tokens'))
    usage = {'input_tokens':inp, 'output_tokens':out, 'total_tokens':total if total is not None and total >= inp + out else inp + out}
    cache = _detail(value, 'cache_read_tokens', ('input_token_details', 'cache_read'),
                    ('prompt_tokens_details', 'cached_tokens'))
    reasoning = _detail(value, 'reasoning_tokens', ('output_token_details', 'reasoning'),
                        ('completion_tokens_details', 'reasoning_tokens'))
    if cache is not None:
        usage['cache_read_tokens'] = min(cache, inp)
    if reasoning is not None:
        usage['reasoning_tokens'] = reasoning
    return usage


def has_usage(usage) -> bool:
    """Whether a usage record says anything: reported calls, or calls that
    came back without a report (those are counted, never priced as free)."""
    return bool(usage and (usage.get('reported_calls') or usage.get('unreported_calls')))


def add_usage(target, usage):
    if usage.get('unreported_calls'):
        target['unreported_calls'] = target.get('unreported_calls', 0) + usage['unreported_calls']
    if 'input_tokens' not in usage:
        return
    for key in ('input_tokens','output_tokens','total_tokens'):
        target[key] = target.get(key, 0) + usage[key]
    for key in DETAIL_KEYS:
        if key in usage:
            target[key] = target.get(key, 0) + usage[key]
    target['reported_calls'] = target.get('reported_calls', 0) + usage.get('reported_calls', 1)
    target['source'] = 'provider'


def priced(usage):
    """What `usage` cost, by the machine's own `llm` package pricing.

    The contract's UsageTracker owns prices (environment-configured), so
    Studio never hard-codes one. None when nothing was reported or the
    package cannot price it.
    """
    if not usage or not usage.get('reported_calls'):
        return None
    try:
        from llm import LLMUsage, UsageTracker
        tracker = UsageTracker()
        tracker.add(LLMUsage(input_tokens=usage.get('input_tokens', 0),
                             output_tokens=usage.get('output_tokens', 0),
                             total_tokens=usage.get('total_tokens', 0),
                             cache_read_tokens=usage.get('cache_read_tokens', 0),
                             reasoning_tokens=usage.get('reasoning_tokens', 0)))
        snapshot = tracker.snapshot() or {}
    except Exception:
        return None
    cost = snapshot.get('total_token_cost', snapshot.get('total_cost'))
    if not isinstance(cost, (int, float)):
        return None
    return {'total_cost': cost,
            **{key: snapshot[key] for key in ('input_price_per_1m', 'output_price_per_1m',
                                              'cached_input_ratio') if key in snapshot}}


def combined(*usages):
    """The sum of several usage records; None when none of them reported any."""
    total = {}
    for usage in usages:
        if has_usage(usage):
            add_usage(total, usage)
    return total or None


def record(state, usage):
    """Add one model call to the run, and to the node running it if any.

    A call the provider reported nothing for is counted as such — the Usage
    tab says how many — rather than dropped, which read as "usage never
    updates" on a provider that sends none."""
    usage = usage or {'unreported_calls': 1}
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
    key = model_bridge.usage_hook(lambda result: record(state, reported_usage(getattr(result, 'usage', None))))
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
