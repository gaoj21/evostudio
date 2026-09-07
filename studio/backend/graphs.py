"""Graph CRUD for EvoAgentX Studio.

Persists canvas graphs as JSON at studio/data/graphs/{id}.json and validates
them by constructing a real SequentialWorkFlowGraph.
"""

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from .studio_config import DATA_DIR

GRAPHS_DIR = DATA_DIR / "graphs"

_lock = threading.Lock()

# Keys kept on a canvas task; everything else (e.g. x/y positions) is stripped
# before handing tasks to SequentialWorkFlowGraph.
TASK_KEYS = [
    "name", "description", "inputs", "outputs", "prompt",
    "system_prompt", "parse_mode", "tool_names",
]

# Canvas-only: `skill_names` is resolved into the node's system prompt at run
# time (skills_api.inject_into_tasks), and `memory` is read by the Studio when
# it writes to the node's store (memory_policy) — neither reaches the framework.


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify(name: str) -> str:
    slug = name.lower().replace(" ", "-").replace("_", "-")
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "graph"


def _graph_path(graph_id: str) -> Path:
    return GRAPHS_DIR / f"{graph_id}.json"


def graph_exists(graph_id: str) -> bool:
    return _graph_path(graph_id).is_file()


def create_graph(name: str, goal: str) -> dict:
    with _lock:
        GRAPHS_DIR.mkdir(parents=True, exist_ok=True)
        graph_id = free_id(name)
        graph = {
            "id": graph_id,
            "name": name or graph_id,
            "goal": goal or "",
            "output_dir": "runs",
            "tasks": [],
            "edges": [],
            "updated_at": _utcnow(),
        }
        _save(graph)
        return graph


def free_id(name: str, taken: str | None = None) -> str:
    """An unused id for a workflow called `name`.

    `taken` is an id the caller already owns, so re-deriving its own id does
    not count as a collision.
    """
    base = _slugify(name or "graph")
    candidate = base
    n = 2
    while candidate != taken and graph_exists(candidate):
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def rename_graph(graph_id: str, name: str) -> dict:
    """Give a workflow a new name, and the identity to match.

    A workflow's id is its identity everywhere: the file on disk, the folder
    its run artifacts land in, its memory stores, the name of the project it
    exports as. Leaving that behind when the workflow is renamed makes the name
    decoration — you call something "Supplier Risk", hand the export to a
    colleague, and they receive untitled-workflow-3.zip.

    Returns the graph under its new id. The old id is not left behind.
    """
    graph = load_graph(graph_id)
    if graph is None:
        raise GraphValidationError([f"Workflow '{graph_id}' not found"])

    new_id = free_id(name, taken=graph_id)
    if new_id == graph_id:
        graph["name"] = name
        with _lock:
            _save(graph)
        return graph

    busy = _in_flight(graph_id)
    if busy:
        raise GraphValidationError([
            f"'{graph_id}' has {busy} in progress. Renaming moves the folders "
            "they are writing to, so let them finish first."
        ])

    with _lock:
        graph["id"] = new_id
        graph["name"] = name
        _save(graph)
        _graph_path(graph_id).unlink(missing_ok=True)
    _move_graph_data(graph_id, new_id)
    return graph


def _in_flight(graph_id: str) -> str:
    """What is still running for a workflow, as a phrase, or an empty string."""
    from . import batch as batch_store
    from . import runner
    runs = sum(1 for r in runner.list_runs(graph_id=graph_id)
               if r.get("status") == "running")
    batches = sum(1 for b in batch_store.list_batches(graph_id=graph_id)
                  if b.get("status") in ("running", "cancelling"))
    parts = []
    if runs:
        parts.append(f"{runs} run{'s' if runs > 1 else ''}")
    if batches:
        parts.append(f"{batches} batch{'es' if batches > 1 else ''}")
    return " and ".join(parts)


def _move_graph_data(old_id: str, new_id: str) -> None:
    """Carry a renamed workflow's own data across.

    Best effort per item: a workflow that has never run has none of these, and
    failing to move one is not a reason to leave the rename half-done.
    """
    import shutil

    from . import memory_store
    from . import stm_store
    from . import table_store
    from . import workspace as workspace_mod
    for old, new in ((workspace_mod.workspace_root(old_id), workspace_mod.workspace_root(new_id)),
                     (memory_store.MEMORY_DIR / old_id, memory_store.MEMORY_DIR / new_id),
                     (table_store.TABLES_DIR / old_id, table_store.TABLES_DIR / new_id),
                     (stm_store.STM_DIR / f"{old_id}.json",
                      stm_store.STM_DIR / f"{new_id}.json")):
        try:
            if old.exists() and not new.exists():
                new.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old), str(new))
        except OSError:
            continue

    _repoint_history(old_id, new_id)


def _repoint_history(old_id: str, new_id: str) -> None:
    """Point this workflow's past runs and batches at its new id.

    Without this its own history disappears from the History panel the moment
    it is renamed — the records are still there, filed under a name that no
    longer exists.
    """
    from . import batch as batch_store
    from . import runner
    for directory in (runner.RUNS_DIR, batch_store.BATCHES_DIR):
        if not directory.is_dir():
            continue
        for path in directory.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if data.get("graph_id") != old_id:
                continue
            data["graph_id"] = new_id
            try:
                path.write_text(json.dumps(data, indent=2, ensure_ascii=False,
                                           default=str), encoding="utf-8")
            except OSError:
                continue
    for store in (runner._runs, batch_store._batches):
        for state in store.values():
            if state.get("graph_id") == old_id:
                state["graph_id"] = new_id


def _save(graph: dict) -> None:
    graph["updated_at"] = _utcnow()
    GRAPHS_DIR.mkdir(parents=True, exist_ok=True)
    with open(_graph_path(graph["id"]), "w", encoding="utf-8") as f:
        json.dump(graph, f, indent=2, ensure_ascii=False)


def load_graph(graph_id: str) -> dict | None:
    path = _graph_path(graph_id)
    if not path.is_file():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def list_graphs() -> list[dict]:
    if not GRAPHS_DIR.is_dir():
        return []
    out = []
    for path in sorted(GRAPHS_DIR.glob("*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                g = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        out.append({
            "id": g.get("id", path.stem),
            "name": g.get("name", ""),
            "goal": g.get("goal", ""),
            "updated_at": g.get("updated_at", ""),
        })
    out.sort(key=lambda g: g.get("updated_at", ""), reverse=True)
    return out


def delete_graph(graph_id: str) -> bool:
    with _lock:
        path = _graph_path(graph_id)
        if not path.is_file():
            return False
        path.unlink()
        return True


def save_graph(graph_id: str, body: dict) -> dict:
    """Persist a PUT body as the graph document.

    Renaming the workflow renames its identity with it, so the returned graph
    may carry a different id from the one in the URL — the caller follows it.
    """
    existing = load_graph(graph_id) or {}
    new_name = body.get("name")
    if existing and new_name and new_name != existing.get("name"):
        # Do this first: everything below writes under the id, and the point of
        # a rename is that those writes land under the new one.
        graph_id = rename_graph(graph_id, new_name)["id"]
    output_dir = (body.get("output_dir") or "runs").strip().strip("/") or "runs"
    if output_dir.startswith("..") or "/../" in f"/{output_dir}/":
        raise GraphValidationError(
            [f"output_dir {output_dir!r} must stay inside the workspace"]
        )
    graph = {
        "id": graph_id,
        "name": body.get("name", existing.get("name", graph_id)),
        "goal": body.get("goal", existing.get("goal", "")),
        "output_dir": output_dir,
        # Name of a custom tool run over each input record before the workflow
        # sees it. Part of the workflow definition, so it travels with exports.
        "preprocess": (body.get("preprocess", existing.get("preprocess")) or None),
        "tasks": body.get("tasks", []),
        "edges": body.get("edges", []),
    }
    with _lock:
        _save(graph)
    return load_graph(graph_id)


class GraphValidationError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def is_source_task(task: dict) -> bool:
    """Canvas input-source node (e.g. credit_risk feed) — not an LLM task."""
    return task.get("kind") == "source"


def is_tool_task(task: dict) -> bool:
    """Canvas tool node (kind="tool", deterministic, no LLM) — runs in Python
    before the framework graph, so it may only depend on source nodes, other
    tool nodes, or workflow inputs."""
    return task.get("kind") == "tool"


def validate_tool_tasks(tasks: list[dict]) -> None:
    """Static checks for canvas tool nodes: the tool must exist, and its
    inputs must not depend on LLM-task outputs (v1 ordering limitation —
    attach the tool to an LLM node instead)."""
    from . import tools_registry
    llm_outputs = {
        out.get("name")
        for t in tasks or [] if not is_source_task(t) and not is_tool_task(t)
        for out in (t.get("outputs") or [])
    }
    for task in tasks or []:
        if not is_tool_task(task):
            continue
        tool_name = task.get("tool")
        if not tool_name or tools_registry.find_tool(tool_name) is None:
            raise GraphValidationError(
                [f"Tool node '{task.get('name')}' references unknown or "
                 f"unavailable tool {tool_name!r}"]
            )
        for inp in task.get("inputs") or []:
            if inp.get("name") in llm_outputs:
                raise GraphValidationError(
                    [f"Tool node '{task.get('name')}' input '{inp.get('name')}' "
                     f"depends on an LLM node's output. Tool nodes run before "
                     f"LLM nodes (v1); attach the tool to an LLM node instead."]
                )


def parked_task_names(tasks: list[dict], edges: list[dict]) -> set:
    """Non-source nodes with no edges at all are 'parked' drafts.

    Experiment workflows often park alternative nodes on the canvas and swap
    connections; parked nodes are excluded from framework validation,
    workflow-input computation and execution until wired in.
    """
    connected = set()
    for e in edges or []:
        connected.add(e.get("source"))
        connected.add(e.get("target"))
    if not connected:
        # No edges anywhere: nothing is "parked" — a single-node graph (or a
        # set of standalone nodes) is a legitimate runnable graph.
        return set()
    return {t.get("name") for t in (tasks or [])
            if not is_source_task(t) and t.get("name") not in connected}


def validate_source_tasks(tasks: list[dict]) -> None:
    """Static checks for canvas source nodes (known type, required config)."""
    from .source_apis import SOURCE_TYPE_SCHEMAS

    for task in tasks or []:
        if not is_source_task(task):
            continue
        config = task.get("source") or {}
        schema = SOURCE_TYPE_SCHEMAS.get(config.get("type"))
        if schema is None:
            raise GraphValidationError(
                [f"Source node '{task.get('name')}' has unknown or missing "
                 f"source type: {config.get('type')!r} "
                 f"(expected one of {sorted(SOURCE_TYPE_SCHEMAS)})"]
            )
        for field in schema["config"]:
            if field.get("required") and not config.get(field["name"]):
                raise GraphValidationError(
                    [f"Source node '{task.get('name')}' requires "
                     f"'{field['name']}' in its source config"]
                )


# Canvas layout: one column per topological depth.
COL_WIDTH = 260
ROW_HEIGHT = 150


def auto_layout(graph: dict) -> None:
    """Place each task in the column of its longest dependency chain.

    Longest path rather than first-seen depth: a node fed by both a source and
    a three-deep chain belongs after the chain, not beside the source.
    """
    tasks = graph.get("tasks", []) or []
    edges = graph.get("edges", []) or []
    names = {t["name"] for t in tasks}
    incoming = {n: [] for n in names}
    for e in edges:
        if e.get("source") in names and e.get("target") in names:
            incoming[e["target"]].append(e["source"])

    depth: dict[str, int] = {}

    def resolve(name: str, seen: frozenset) -> int:
        if name in depth:
            return depth[name]
        if name in seen:  # a cycle cannot be laid out by depth; break it here
            return 0
        parents = incoming.get(name, [])
        d = 0 if not parents else 1 + max(resolve(p, seen | {name}) for p in parents)
        depth[name] = d
        return d

    for name in names:
        resolve(name, frozenset())

    per_column: dict[int, int] = {}
    for task in sorted(tasks, key=lambda t: (depth.get(t["name"], 0), t["name"])):
        d = depth.get(task["name"], 0)
        row = per_column.get(d, 0)
        per_column[d] = row + 1
        task["x"] = d * COL_WIDTH
        task["y"] = row * ROW_HEIGHT


def subgraph_from(tasks: list[dict], edges: list[dict],
                  start_at: list[str]) -> tuple[list[dict], list[dict]]:
    """The part of the graph reachable from `start_at`, those nodes included.

    Starting mid-pipeline means the upstream nodes simply are not part of this
    run: whatever they would have produced becomes an input the caller supplies
    (usually the output of an earlier run). Unknown names are ignored so a
    stale selection cannot make a run impossible to start.
    """
    if not start_at:
        return tasks, edges
    by_name = {t.get("name"): t for t in tasks}
    starts = [n for n in start_at if n in by_name]
    if not starts:
        raise GraphValidationError(
            [f"None of {start_at} is a node in this workflow"]
        )

    outgoing: dict[str, list[str]] = {}
    for edge in edges or []:
        outgoing.setdefault(edge.get("source"), []).append(edge.get("target"))

    reachable: set[str] = set()
    queue = list(starts)
    while queue:
        name = queue.pop()
        if name in reachable or name not in by_name:
            continue
        reachable.add(name)
        queue.extend(outgoing.get(name) or [])

    kept_tasks = [t for t in tasks if t.get("name") in reachable]
    kept_edges = [e for e in (edges or [])
                  if e.get("source") in reachable and e.get("target") in reachable]
    return kept_tasks, kept_edges


def validate_task_skills(tasks: list[dict]) -> None:
    """Reject references to skills that no longer exist."""
    from . import skills_api
    try:
        skills_api.validate_skill_names(tasks)
    except skills_api.SkillError as e:
        raise GraphValidationError([str(e)])


def validate_task_memory(tasks: list[dict]) -> None:
    """Reject memory settings that would quietly keep nothing."""
    from . import memory_policy
    siblings = {t.get("name"): t for t in tasks if t.get("name")}
    errors = [e for task in tasks for e in memory_policy.validate(task, siblings)]
    if errors:
        raise GraphValidationError(errors)


def strip_task(task: dict) -> dict:
    """Remove UI-only fields (x/y, skill_names, memory) from a canvas task."""
    return {k: task[k] for k in TASK_KEYS if k in task and task[k] is not None}


def topo_sort_tasks(tasks: list[dict], edges: list[dict]) -> list[dict]:
    """Order tasks by topological sort of the canvas edges.

    Ties are broken by the input order of the tasks list (Kahn's algorithm
    with the smallest original index first). Edges referencing unknown tasks
    or forming a cycle raise GraphValidationError.
    """
    index = {t.get("name"): i for i, t in enumerate(tasks)}
    if None in index:
        raise GraphValidationError(["Every task must have a name."])
    if len(index) != len(tasks):
        raise GraphValidationError(["Duplicate task names are not allowed."])

    in_degree = {name: 0 for name in index}
    children = {name: [] for name in index}
    errors = []
    seen = set()
    for edge in edges or []:
        src, tgt = edge.get("source"), edge.get("target")
        if src not in index:
            errors.append(f"Edge source '{src}' does not match any task name.")
            continue
        if tgt not in index:
            errors.append(f"Edge target '{tgt}' does not match any task name.")
            continue
        if (src, tgt) in seen:
            continue
        seen.add((src, tgt))
        children[src].append(tgt)
        in_degree[tgt] += 1
    if errors:
        raise GraphValidationError(errors)

    ready = sorted([n for n, d in in_degree.items() if d == 0], key=index.get)
    ordered = []
    while ready:
        name = ready.pop(0)
        ordered.append(name)
        for child in children[name]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                ready.append(child)
        ready.sort(key=index.get)

    if len(ordered) != len(tasks):
        remaining = [n for n in index if n not in ordered]
        raise GraphValidationError(
            [f"Edges contain a cycle involving tasks: {remaining}"]
        )

    by_name = {t["name"]: t for t in tasks}
    return [by_name[n] for n in ordered]


def compute_workflow_inputs(ordered_tasks: list[dict],
                            edges: list[dict] | None = None) -> list[dict]:
    """Task inputs not matched by any upstream task output name (topo order).

    Outputs of source nodes with no outgoing edge do NOT count: the runner
    skips unconnected source nodes, so their outputs never materialize and
    the fields must be asked for as workflow inputs instead.
    """
    wired = {e.get("source") for e in (edges or [])}
    parked = parked_task_names(ordered_tasks, edges)
    produced = set()
    workflow_inputs = []
    seen = set()
    for task in ordered_tasks:
        if task.get("name") in parked:
            continue  # parked draft node: inert, its inputs are not asked for
        for inp in task.get("inputs", []) or []:
            if inp.get("name") in produced or inp.get("name") in seen:
                continue
            seen.add(inp["name"])
            workflow_inputs.append({
                "name": inp["name"],
                "type": inp.get("type", "str"),
                "description": inp.get("description", ""),
                "required": inp.get("required", True),
                # The first task that needs it, so the run form can say what
                # each value is actually for.
                "consumed_by": task.get("name"),
            })
        if is_source_task(task) and task.get("name") not in wired:
            continue
        for out in task.get("outputs", []) or []:
            produced.add(out.get("name"))
    return workflow_inputs


def validate_graph(graph: dict) -> tuple[list[dict], list[dict]]:
    """Validate a canvas graph against the EvoAgentX framework.

    Source nodes (kind="source") and tool nodes (kind="tool") are excluded
    from the framework task list but keep their place in the topological
    order, so their outputs count as produced for workflow_inputs
    computation.

    Returns (ordered_tasks, workflow_inputs) on success; raises
    GraphValidationError with the framework's error messages on failure.
    """
    from evoagentx.workflow.workflow_graph import SequentialWorkFlowGraph

    tasks = graph.get("tasks", []) or []
    edges = graph.get("edges", []) or []
    validate_source_tasks(tasks)
    validate_tool_tasks(tasks)
    validate_task_skills(tasks)
    validate_task_memory(tasks)
    ordered = topo_sort_tasks(tasks, edges)
    parked = parked_task_names(tasks, edges)
    framework_tasks = [strip_task(t) for t in ordered
                       if not is_source_task(t) and not is_tool_task(t)
                       and t["name"] not in parked]
    if framework_tasks:
        try:
            SequentialWorkFlowGraph(goal=graph.get("goal", ""), tasks=framework_tasks)
        except GraphValidationError:
            raise
        except Exception as e:
            raise GraphValidationError([str(e)])
    return ordered, compute_workflow_inputs(ordered, edges)
