"""Which batch records must run in order because of memory.

A node that reads memory another run wrote only sees that run's result if
the run finished first. Run a batch's records in parallel and a record can
miss what the record before it concluded, or see it only sometimes. This
decides the ordering from the memory configuration itself, whatever the
data is about:

- memory matched on a field (customer, ticket, device…): records sharing a
  value of that field run one after another, in batch order; records with
  different values still run in parallel;
- memory not split by any field (recent records, semantic recall, a shared
  space): every record may read every earlier one, so the batch runs in
  order;
- no node reads memory that a node in this workflow writes: no ordering.

A DataLoader group, or the trajectory field an Input type declares, keeps
its own grouping, which already runs each sequence in order.
"""
from . import memory_policy


def _memory_tasks(graph):
    return [t for t in (graph.get("tasks") or [])
            if t.get("use_long_term_memory") and t.get("enabled") is not False]


def _record_field(reference, graph):
    """The record field a match binding reads, or None when only a run can
    produce it (an LLM or tool node's output)."""
    tasks = {t.get("name"): t for t in graph.get("tasks") or []}
    if reference.startswith("nodes."):
        for name, task in tasks.items():
            for side in ("outputs", "inputs"):
                prefix = f"nodes.{name}.{side}."
                if reference.startswith(prefix):
                    field = reference[len(prefix):]
                    if side == "outputs" and task.get("kind") == "source":
                        return field
                    if side == "inputs":
                        return field
                    return None
        return None
    root = reference.split(".")[0]
    for task in tasks.values():
        if task.get("kind") in ("source",):
            continue
        if any(o.get("name") == root for o in task.get("outputs") or []):
            return None      # produced mid-run: not known before the record runs
    return reference


def plan(graph):
    """{"mode": None|"entity"|"all", "field": ..., "reason": ...}."""
    tasks = _memory_tasks(graph)
    if not tasks:
        return {"mode": None, "field": None, "reason": "no node uses memory"}
    writers = {t.get("name") for t in tasks if memory_policy.policy(t)["write_enabled"]}
    readers = []
    for t in tasks:
        p = memory_policy.policy(t)
        if not p["read_enabled"] or p["retrieve"] == 0:
            continue
        sources = p["read_from"] if p["read_from"] is not None else [t.get("name")]
        if (t.get("memory") or {}).get("provider") == "mem0":
            sources = list(writers)          # one shared space
        if writers & set(sources):
            readers.append((t, p))
    if not readers:
        return {"mode": None, "field": None, "reason": "no node reads memory written in this workflow"}
    fields = set()
    for task, p in readers:
        raw = task.get("memory") or {}
        match = p.get("match") if raw.get("provider") != "mem0" else None
        if not match:
            return {"mode": "all", "field": None,
                    "reason": f"'{task.get('name')}' reads memory across all records, so each record may read every earlier one"}
        field = _record_field(match, graph)
        if field is None:
            return {"mode": "all", "field": None,
                    "reason": f"'{task.get('name')}' matches memory on '{match}', which only exists once a record has run"}
        fields.add(field)
    if len(fields) > 1:
        return {"mode": "all", "field": None,
                "reason": f"memory is matched on different fields ({', '.join(sorted(fields))})"}
    field = fields.pop()
    return {"mode": "entity", "field": field,
            "reason": f"memory is matched on '{field}': records with the same {field} run in order"}


def key(sequence, record):
    """The ordering key of one record under `sequence`, or None to run freely."""
    mode = (sequence or {}).get("mode")
    if mode == "all":
        return "memory:all"
    if mode == "entity":
        from .bindings import resolve
        value = resolve(record or {}, sequence["field"])
        if value is None or value == "":
            return None
        return f"memory:{sequence['field']}={value}"
    return None
