"""Memory metadata bindings, independent of the stored prompt content.

A binding is a legacy unqualified field, or nodes.<name>.outputs.<field> /
nodes.<name>.inputs.<field>. Qualified fields never fall back to another node.
"""
from datetime import datetime, timezone


class BindingError(ValueError):
    pass


def resolve(data, reference):
    if not reference:
        return None
    if reference in data:
        return data[reference]
    for prefix in sorted(data, key=len, reverse=True):
        if reference.startswith(prefix + '.'):
            return _walk(data[prefix], reference[len(prefix) + 1:].split('.'))
    return _walk(data, reference.split('.'))


def _walk(value, keys):
    import json
    for key in keys:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return None
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def timestamp(value):
    """Comparable UTC ISO value; business time is never replaced by wall time."""
    if value is None or value == '':
        return ''
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec='microseconds')
    except (TypeError, ValueError) as exc:
        raise BindingError(f'Memory time must be an ISO date or datetime, got {value!r}.') from exc


def declared(reference, task, siblings):
    if reference.startswith('nodes.'):
        # Match node names before splitting: a node name may itself contain dots.
        for name, node in siblings.items():
            for side in ('inputs', 'outputs'):
                prefix = f'nodes.{name}.{side}.'
                if reference.startswith(prefix):
                    root = reference[len(prefix):].split('.')[0]
                    return root in {p.get('name') for p in node.get(side, [])}
        return False
    root = reference.split('.')[0]
    return any(root == p.get('name') for node in siblings.values()
               for side in ('inputs', 'outputs') for p in node.get(side, []))


def runtime_context(state, flat=None):
    data = dict(flat if flat is not None else state.get('_effective_inputs') or {})
    # Exact qualified aliases support punctuation in node names.
    for name, io in state.get('_node_io', {}).items():
        for side, values in (('inputs', io.get('inputs')), ('outputs', io.get('output'))):
            for field, value in (values or {}).items():
                data[f'nodes.{name}.{side}.{field}'] = value
    return data


def _time_filtered(memory):
    if 'time_filter' in memory:
        return bool(memory['time_filter'])
    return memory.get('version') != 2 and bool(memory.get('at'))


def binding_inputs(tasks):
    """Workflow inputs that memory bindings need and no node produces.

    Memory is configured by field name — "match on customer", "dated by
    created_at" — and those fields belong to the data, not to any one
    pipeline. When no node declares the field it is taken as an input of
    the workflow, so the run form asks for it and a batch keeps that column.
    Always optional: memory is an aid, and a missing binding value skips the
    memory operation with a note rather than stopping the run.
    Qualified references (nodes.<name>.…) name a node explicitly and are
    never turned into inputs.
    """
    produced = {p.get('name') for t in tasks or []
                for side in ('inputs', 'outputs') for p in (t.get(side) or [])}
    out, seen = [], set()
    for task in tasks or []:
        memory = task.get('memory') or {}
        if not task.get('use_long_term_memory') or task.get('enabled') is False or not isinstance(memory, dict):
            continue
        for label, reference in (('entity', memory.get('match')),
                                 ('time', memory.get('at')),
                                 ('unique key', memory.get('key') if memory.get('write_mode') == 'upsert' else None)):
            if not reference or not isinstance(reference, str) or reference.startswith('nodes.'):
                continue
            root = reference.split('.')[0]
            if not root or root in produced or root in seen:
                continue
            seen.add(root)
            out.append({'name': root, 'type': 'str', 'required': False,
                        'description': f"Memory {label} for '{task.get('name')}'"
                                       + (' (reads only earlier records)' if label == 'time' and _time_filtered(memory) else ''),
                        'consumed_by': task.get('name'), 'memory_binding': label})
    return out
