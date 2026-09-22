"""The execution plan: one immutable description of what a run will do.

A run used to be shaped by three rules that never met — the canvas edges, the
backend's input derivation, and the framework's own same-name inference — so
the dialog could show one set of inputs while the request ran another, and a
run could be created and then die on a value nobody had been asked for.

Phase 1 of RUN_INPUT_FLOW_PLAN.md: everything a run needs is computed here,
once, from a graph revision; the result carries an id; and a start request
must present that id and pass every check *before* a run exists. The
underlying executor is unchanged at this stage — the point is that what you
confirmed is what runs.
"""

import copy
import hashlib
import json

from backend.api import graphs as graph_store
from backend.api import tools_registry
from backend.api import memory_policy

# The parts of a graph that change what a run does. Position on the canvas
# and the display name do not, so moving a node does not invalidate a plan.
_REVISION_FIELDS = ("goal", "tasks", "edges", "preprocess", "output_dir")

SINGLE_POLICY = "first"


def revision_of(graph: dict) -> str:
    """A content hash of everything that affects execution."""
    canonical = json.dumps({k: graph.get(k) for k in _REVISION_FIELDS},
                           sort_keys=True, default=str, ensure_ascii=False)
    return "rev:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _kind(task: dict) -> str:
    if graph_store.is_source_task(task):
        return "source"
    if graph_store.is_tool_task(task):
        return "tool"
    return "llm"


def _does_work(task: dict) -> bool:
    """A source only yields data; the run has to reach something that acts on it."""
    return _kind(task) != "source" and task.get("kind") != "evaluator"


def _reaches_work(tasks, edges, name: str) -> bool:
    """Would starting at `name` execute at least one LLM or tool node?"""
    try:
        kept, kept_edges = graph_store.subgraph_from(tasks, edges, [name])
    except graph_store.GraphValidationError:
        return False
    parked = graph_store.parked_task_names(kept, kept_edges)
    return any(_does_work(t) and t.get("name") not in parked for t in kept)


def _yields_trajectories(type_) -> bool:
    from backend.features import plugins
    return bool((plugins.source_schemas().get(type_) or {}).get("sequence"))


def _source_of(tasks, edges) -> dict | None:
    """The wired canvas source, described by its own configuration.

    Not probed: a plan must be cheap and must not depend on a dataset being
    present. How many records it yields is what it was configured to yield.
    """
    wired = {e.get("source") for e in edges or []}
    from backend.features.data.input_composition import is_reference
    ordered = sorted(tasks, key=is_reference)
    for task in ordered:
        if not graph_store.is_source_task(task) or task.get("name") not in wired:
            continue
        config = task.get("source") or {}
        raw = config.get("n")
        try:
            n = int(raw) if raw is not None else 1
        except (TypeError, ValueError):
            n = 1
        return {
            "node": task.get("name"),
            "type": config.get("type"),
            # n=0 means "all records", whose number a plan does not know; nor
            # does it for an Input whose n counts trajectories, not records.
            "cardinality": None if _yields_trajectories(config.get("type")) else n if n > 0 else None,
            "single_policy": SINGLE_POLICY,
        }
    return None


def compile_plan(graph: dict, start_at=None, mode: str = "single") -> dict:
    """Validate the graph and describe the run it would produce.

    Raises GraphValidationError / ToolResolveError for a graph that cannot
    run, including a start point from which nothing would execute.
    """
    # Whatever shape it arrived in, the plan is compiled from the migrated
    # form: explicit mappings, explicit enabled flags. Validation works on a
    # copy of its own, so this is the copy the rest of the plan reads.
    graph = graph_store.migrate_flow(copy.deepcopy(graph))
    graph_store.validate_graph(graph, check_memory=False)

    tasks = graph.get("tasks") or []
    edges = graph.get("edges") or []
    start_at = [n for n in (start_at or []) if n]
    kept, kept_edges = graph_store.subgraph_from(tasks, edges, start_at)
    ordered = graph_store.topo_sort_tasks(kept, kept_edges)
    parked = graph_store.parked_task_names(kept, kept_edges)
    # A source wired to nothing is skipped by the runner; a plan that listed
    # it would describe a run that does not happen.
    wired = {e.get("source") for e in kept_edges}
    active = [t for t in ordered if t.get("name") not in parked and t.get("kind") != "evaluator"
              and not (graph_store.is_source_task(t) and t.get("name") not in wired)]

    if not any(_does_work(t) for t in active):
        where = f" from {start_at}" if start_at else ""
        raise graph_store.GraphValidationError(
            [f"Nothing would run{where}: no LLM or tool node is reached."])

    tools_registry.validate_tool_names(sorted({
        name for t in active for name in (t.get("tool_names") or [])
    }))

    live_edges = [e for e in kept_edges
                  if e.get("source") in {t.get("name") for t in active}
                  and e.get("target") in {t.get("name") for t in active}]
    incoming: dict[str, list[str]] = {}
    for e in live_edges:
        incoming.setdefault(e.get("target"), []).append(e.get("source"))
    # The edges say what feeds what; a run reads nothing else.
    bindings = graph_store.compile_bindings(active, live_edges)
    nodes = [{
        "name": t.get("name"),
        "kind": _kind(t),
        "depends_on": sorted(set(incoming.get(t.get("name"), []))),
        "input_bindings": bindings.get(t.get("name"), {}),
        "output_names": [o.get("name") for o in (t.get("outputs") or [])],
    } for t in active]

    inputs = graph_store.compute_workflow_inputs(ordered, kept_edges)
    source = _source_of(active, kept_edges)

    warnings = []
    by_name = {t.get("name"): t for t in tasks}
    for task in active:
        settings = memory_policy.policy(task)
        if task.get("use_long_term_memory") and (task.get("memory") or {}).get("provider") == "mem0":
            from backend.api.mem0_service import get_space
            try:
                get_space(graph, task['memory'].get('space_id'))
            except ValueError as exc:
                raise graph_store.GraphValidationError([str(exc)]) from exc
            continue
        if not task.get("use_long_term_memory") or not settings["read_enabled"] or not settings["retrieve"]:
            continue
        names = settings["read_from"] if settings["read_from"] is not None else [task.get("name")]
        for name in names:
            other = by_name.get(name)
            if other is None:
                warnings.append(f"Memory for '{task['name']}': source '{name}' does not exist.")
                continue
            source_settings = memory_policy.policy(other)
            if settings["kind"] != source_settings["kind"]:
                warnings.append(f"Memory for '{task['name']}': '{name}' uses {source_settings['kind']} storage, but this reader uses {settings['kind']}. Align storage types to read its history.")
            elif settings["kind"] == "table" and settings["match"] != source_settings["match"]:
                warnings.append(f"Memory for '{task['name']}': '{name}' uses subject field '{source_settings['match']}', but this reader uses '{settings['match']}'. Verify that both identify the same subject.")
    if mode == "single" and source and (source["cardinality"] or 0) > 1:
        n = source["cardinality"]
        warnings.append(
            f"Source '{source['node']}' yields {n} records; a single run takes "
            f"the {SINGLE_POLICY} one. Use a batch run to run all {n}.")

    # What this run leaves out — nodes before the start point, parked drafts,
    # sources wired to nothing — so the dialog can say so instead of the run
    # silently being smaller than the canvas.
    running = {n["name"] for n in nodes}
    skipped = [t.get("name") for t in tasks if t.get("name") not in running]

    revision = revision_of(graph)
    plan_id = "plan:" + hashlib.sha256(
        f"{revision}|{json.dumps(start_at)}|{mode}".encode("utf-8")).hexdigest()[:16]
    return {
        "plan_id": plan_id,
        "graph_id": graph.get("id"),
        "graph_revision": revision,
        "mode": mode,
        "start_at": start_at,
        "nodes": nodes,
        "skipped": skipped,
        "inputs": inputs,
        "source": source,
        # Offered only where starting would run something. The full graph is
        # consulted, not the cut one: these are the choices for the dialog.
        "start_points": [t.get("name") for t in tasks
                         if _reaches_work(tasks, edges, t.get("name"))],
        "warnings": warnings,
    }


_TYPES = {
    "str": (str,), "string": (str,),
    "int": (int,), "integer": (int,),
    "float": (int, float), "number": (int, float),
    "bool": (bool,), "boolean": (bool,),
    "list": (list,), "array": (list,),
    "dict": (dict,), "object": (dict,), "json": (dict, list),
}


def check_inputs(plan: dict, inputs: dict) -> list[str]:
    """Why these inputs cannot start this plan; empty when they can.

    The framework validates them too — inside the run thread, after a run id
    has been handed back. Doing it here is what lets a refusal come first.
    """
    errors = []
    given = inputs or {}
    for spec in plan.get("inputs") or []:
        name = spec.get("name")
        value = given.get(name)
        if value is None or value == "":
            if spec.get("required", True):
                errors.append(f"Missing required input '{name}' "
                              f"(needed by '{spec.get('consumed_by')}').")
            continue
        wanted = _TYPES.get(str(spec.get("type") or "str").lower())
        if wanted is None:
            continue
        ok = isinstance(value, wanted)
        # bool is an int to Python; it is not to a node that asked for a number.
        if ok and isinstance(value, bool) and bool not in wanted:
            ok = False
        if not ok:
            errors.append(f"Input '{name}' must be {spec.get('type')}, "
                          f"got {type(value).__name__}.")
    return errors


# How much of a record the "choose a record" list shows: enough to tell them
# apart, not the news batch.
_SUMMARY_FIELDS = 6
_SUMMARY_WIDTH = 80


def summarise_record(record: dict) -> dict:
    """The short scalar fields of a record, for picking one out of a list."""
    out = {}
    for key, value in (record or {}).items():
        if isinstance(value, (dict, list)):
            continue
        text = str(value)
        if len(text) > _SUMMARY_WIDTH:
            continue
        out[key] = value
        if len(out) >= _SUMMARY_FIELDS:
            break
    return out


def records_of(graph: dict, plan: dict) -> list[dict]:
    """The source's records, summarised and indexed, for an explicit choice.

    This probes the dataset, which is why it is not part of compile_plan:
    a plan is asked for on every edit, this only when someone asks to pick.
    """
    from backend.api import sources

    source = plan.get("source")
    if not source:
        return []
    node = next((t for t in graph.get("tasks") or []
                 if t.get("name") == source["node"]), None)
    if node is None:
        return []
    from backend.features.data.input_composition import load_primary
    rows = load_primary(graph, node) if (node.get('source') or {}).get('type') == 'dataloader' else sources.records_from_source_node(node)
    return [{"index": i, "summary": summarise_record(r)} for i, r in enumerate(rows)]
