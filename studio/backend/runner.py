"""Run engine for EvoAgentX Studio.

Runs execute in daemon threads; state lives in a module-level dict and is
persisted to studio/data/runs/{run_id}.json on completion. Node status is read
live off the executing WorkFlowGraph; node outputs are captured after the run
from the workflow Environment's execution data (best effort per output name).
"""

import json
import sys
import threading
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from llm import get_evoagentx_llm

import memory_policy
import memory_store
import table_store
import stm_store
import review as review_mod
import sources
import tools_registry
import workspace as workspace_mod
from graphs import (is_source_task, is_tool_task, parked_task_names,
                    strip_task, subgraph_from, topo_sort_tasks)
import skills_api
from studio_config import data_path

RUNS_DIR = data_path("runs")

_runs: dict[str, dict] = {}
_lock = threading.Lock()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_llm():
    return get_evoagentx_llm()  # default provider from llm/providers.json


# Per-store LTM locks: concurrent runs of the same graph (parallel batch
# items, watcher fires) share a store dir — FAISS/SQLite writes must be
# serialized to avoid corruption.
_ltm_locks: dict[str, threading.Lock] = {}
_ltm_locks_guard = threading.Lock()


def _ltm_lock(key: str) -> threading.Lock:
    with _ltm_locks_guard:
        return _ltm_locks.setdefault(key, threading.Lock())


def graph_store_subgraph(graph: dict, start_at: list[str]):
    return subgraph_from(graph.get("tasks") or [], graph.get("edges") or [], start_at)


def start_run(graph: dict, inputs: dict, background: bool = True, gray_zone=None,
              run_id: str | None = None, start_at: list[str] | None = None,
              session: str | None = None) -> str:
    """Start a run for a canvas graph; returns the run_id.

    With background=True (default) the run executes in a daemon thread;
    with background=False it executes synchronously on the caller's thread
    (used by the batch runner, which serializes records itself).
    gray_zone: optional (lo, hi) score band routing alert decisions to review.
    run_id: optional pre-assigned id (batch runner sets it before execution
    so live node progress is visible while the item runs).
    start_at: optional node names to begin from — everything upstream is left
    out of this run and its outputs must be supplied as inputs instead.
    session: optional short-term memory session. Runs sharing one are reminded
    of what the others did, in order. Without it nothing is shared: two records
    of a batch are usually independent.
    """
    if start_at:
        tasks, edges = graph_store_subgraph(graph, start_at)
        graph = {**graph, "tasks": tasks, "edges": edges}
    run_id = run_id or uuid.uuid4().hex[:12]
    state = {
        "run_id": run_id,
        "graph_id": graph.get("id"),
        "status": "running",
        "error": None,
        "nodes": [
            {"name": t.get("name"), "status": "pending", "output": None}
            for t in graph.get("tasks", []) or []
        ],
        "result": None,
        "inputs": inputs or {},
        "review_status": None,
        "created_at": _utcnow(),
        "session": (session or "").strip() or None,
        "_graph": None,  # live WorkFlowGraph, set once execution starts
        "_gray_zone": gray_zone,
    }
    with _lock:
        _runs[run_id] = state
    # Written before execution starts, not only when it ends: a run that the
    # process never finishes (a restart, a crash) would otherwise vanish, and
    # the client polling it gets a 404 for something it just started.
    _persist_run(state)
    if background:
        thread = threading.Thread(
            target=_execute_run, args=(run_id, graph, inputs or {}), daemon=True
        )
        thread.start()
    else:
        _execute_run(run_id, graph, inputs or {})
    return run_id


def _set_status(state: dict, status: str, **fields) -> None:
    """Record a run's outcome, unless the user has already walked away from it.

    An abandoned run keeps its own status: the thread it left behind cannot be
    killed, so it goes on to succeed or fail minutes later, and that late
    verdict must not overwrite what the user was told.
    """
    if state.get("_abandoned"):
        # Namespaced, so the abandonment message and status both survive: the
        # user is told the run was dropped, and can still see what it went on
        # to produce.
        state["finished_after_abandon"] = status
        for key, value in fields.items():
            state[f"late_{key}"] = value
        return
    state["status"] = status
    state.update(fields)


def abandon_run(run_id: str) -> dict:
    """Stop reporting on a run. It keeps executing — nothing can stop it.

    `WorkFlow.execute()` is a single blocking call into the framework with no
    interruption point, so there is no way to take back the LLM calls already
    in flight. This is deliberately named for what it does: the run is dropped
    from the UI and stops claiming to be in progress, while its thread runs to
    completion in the background and pays for whatever is left.
    """
    with _lock:
        state = _runs.get(run_id)
        if state is None:
            persisted = _read_run(run_id)
            if persisted is None:
                return {"abandoned": False, "reason": "no such run"}
            if persisted.get("status") == "running":
                # Left behind by an earlier process: nothing is executing, so
                # this is a plain tidy-up rather than an abandonment.
                persisted["status"] = "abandoned"
                persisted["error"] = "Abandoned by the user."
                _persist_run(persisted)
                return {"abandoned": True, "still_executing": False}
            return {"abandoned": False, "reason": f"run already {persisted.get('status')}"}
        if state.get("status") != "running":
            return {"abandoned": False, "reason": f"run already {state.get('status')}"}
        state["_abandoned"] = True
        state["status"] = "abandoned"
        state["error"] = ("Abandoned by the user. The workflow was already "
                          "executing and cannot be interrupted, so it finishes "
                          "in the background — any LLM calls it has left still "
                          "count against your quota.")
        snapshot = dict(state)
    _persist_run(snapshot)
    return {"abandoned": True, "still_executing": True}


def _execute_run(run_id: str, graph_doc: dict, inputs: dict) -> None:
    from evoagentx.agents.agent_manager import AgentManager
    from evoagentx.workflow.workflow import WorkFlow
    from evoagentx.workflow.workflow_graph import SequentialWorkFlowGraph

    state = _runs[run_id]
    graph = None
    wf = None
    try:
        llm = _make_llm()
        ordered = topo_sort_tasks(
            graph_doc.get("tasks", []) or [], graph_doc.get("edges", []) or []
        )
        source_nodes = [t for t in ordered if is_source_task(t)]
        ordered = [t for t in ordered if not is_source_task(t)]
        # Only execute source nodes wired into the pipeline; an unconnected
        # source must not run (and must not be able to fail the run).
        wired = {e.get("source") for e in (graph_doc.get("edges") or [])}
        active_sources = [t for t in source_nodes if t.get("name") in wired]
        skipped_sources = [t for t in source_nodes if t.get("name") not in wired]
        inputs = _run_source_nodes(active_sources, inputs, state)
        state["_effective_inputs"] = dict(inputs)
        # Parked nodes (no edges at all) are inert drafts: not executed.
        parked = parked_task_names(graph_doc.get("tasks", []) or [],
                                   graph_doc.get("edges") or [])
        # Tool nodes (kind="tool"): deterministic, no LLM. v1 runs them after
        # source nodes and before the framework graph — their inputs may only
        # come from sources, other tool nodes, or workflow inputs (enforced at
        # save time by graphs.validate_tool_tasks).
        parked_tools = [t for t in ordered if is_tool_task(t) and t["name"] in parked]
        tool_nodes = [t for t in ordered if is_tool_task(t) and t["name"] not in parked]
        ordered = [t for t in ordered if not is_tool_task(t)]
        inputs = _run_tool_nodes(tool_nodes, inputs, state)
        state["_effective_inputs"] = dict(inputs)
        state["_source_nodes"] = state.get("_source_nodes", []) + [
            {"name": t.get("name"), "status": "skipped",
             "output": {"note": "not connected to the pipeline — not executed"}}
            for t in skipped_sources
        ]
        parked_infos = [
            {"name": t.get("name"), "status": "skipped",
             "output": {"note": "parked (no connections) — not executed"}}
            for t in (ordered + parked_tools) if t.get("name") in parked
        ]
        ordered = [t for t in ordered if t.get("name") not in parked]
        state["_source_nodes"] = state.get("_source_nodes", []) + parked_infos
        if not ordered:
            raise sources.SourceError(
                "Graph has no connected task nodes to run "
                "(all task nodes are parked or it only has source nodes)."
            )
        memories, ordered = _prepare_ltm(graph_doc, ordered, inputs, state)
        # Skills are standing instructions, not tools: resolve them into each
        # node's system prompt here so the framework runs an ordinary graph.
        tasks = [strip_task(t) for t in skills_api.inject_into_tasks(ordered)]
        graph = SequentialWorkFlowGraph(goal=graph_doc.get("goal", ""), tasks=tasks)
        state["_graph"] = graph
        state["nodes"] = state.get("_source_nodes", []) + [
            {"name": node.name, "status": "pending", "output": None}
            for node in graph.nodes
        ]

        agent_manager = AgentManager()
        tool_names = sorted({
            name
            for t in ordered
            for name in (t.get("tool_names") or [])
        })
        agent_manager.add_agents_from_workflow(
            graph, llm_config=llm.config,
            tools=tools_registry.resolve_tools(
                tool_names,
                workspace_dir=workspace_mod.files_dir(graph_doc.get("id", "graph")),
            ) or None,
        )
        _attach_ltm(agent_manager, graph, graph_doc, memories, state)
        wf = WorkFlow(graph=graph, agent_manager=agent_manager, llm=llm)

        result = wf.execute(inputs=inputs)
        _snapshot_nodes(state, graph, wf, succeeded=(result.status == "success"))
        if result.status == "success":
            _set_status(state, "success", result=result.result)
            _save_ltm(graph_doc, memories, graph, wf, state)
            try:
                review = review_mod.maybe_create_review(state, gray_zone=state.get("_gray_zone"))
                if review:
                    state["review_status"] = "awaiting_review"
                    state["review_id"] = review["review_id"]
            except Exception:
                state["memory_error"] = traceback.format_exc()
        else:
            _set_status(state, "failed",
                        error=result.error_msg or result.displayable_error or "run failed")
            # A node set to remember failed runs too: the nodes that did
            # complete before the failure have real outputs worth keeping, and
            # what went wrong is often the thing worth remembering.
            _save_ltm(graph_doc, memories, graph, wf, state, succeeded=False)
    except Exception:
        _set_status(state, "failed", error=traceback.format_exc())
        if graph is not None:
            _snapshot_nodes(state, graph, wf, succeeded=False)
        elif state.get("_source_nodes"):
            # failure before graph construction (e.g. a source node): surface
            # the source nodes' final status in the run view
            by_name = {n["name"]: n for n in state["_source_nodes"]}
            state["nodes"] = [by_name.get(n["name"], n) for n in state["nodes"]]
    finally:
        _persist_run(state)
        try:
            state["_save_output_flags"] = {
                t.get("name"): t.get("save_output", True)
                for t in (graph_doc.get("tasks") or [])
            }
            workspace_mod.write_run_artifacts(
                graph_doc.get("id", "graph"), state,
                output_dir=graph_doc.get("output_dir") or "runs",
            )
        except Exception:
            pass


def _run_source_nodes(source_nodes: list[dict], inputs: dict, state: dict) -> dict:
    """Execute canvas source nodes in Python; merge records into run inputs.

    Explicitly provided inputs win over sampled fields. Source nodes are
    reported as completed nodes with their (truncated) record as output.
    Raises sources.SourceError on unknown type / missing dataset.
    """
    merged = dict(inputs or {})
    infos = []
    records = {}
    for node in source_nodes:
        name = node.get("name")
        try:
            record = sources.records_from_source_node(node)[0]
        except Exception as e:
            infos.append({"name": name, "status": "failed",
                          "output": {"error": str(e)}})
            state["_source_nodes"] = infos
            raise
        for key, value in record.items():
            merged.setdefault(key, value)
        records[name] = record
        infos.append({
            "name": name,
            "status": "completed",
            "output": {
                k: (str(merged[k])[:300] + "…" if len(str(merged[k])) > 300 else merged[k])
                for k in record
            },
        })
    state["_source_nodes"] = infos
    state["_source_records"] = records
    return merged


def _run_tool_nodes(tool_nodes: list[dict], inputs: dict, state: dict) -> dict:
    """Execute canvas tool nodes (kind="tool") deterministically in topo order.

    Args come from the merged inputs (source records / workflow inputs) and
    earlier tool nodes' outputs. Full results are kept for the workspace;
    failures mark the node failed and fail the run.
    """
    merged = dict(inputs or {})
    infos = []
    records = dict(state.get("_source_records") or {})
    for node in tool_nodes:
        name = node.get("name")
        tool_name = node.get("tool")
        try:
            args = {}
            missing = []
            for p in node.get("inputs") or []:
                pname = p.get("name")
                if pname in merged:
                    args[pname] = merged[pname]
                elif p.get("required", True):
                    missing.append(pname)
            if missing:
                raise sources.SourceError(
                    f"Tool node '{name}' is missing inputs: {missing}"
                )
            result = tools_registry.call_tool(
                tool_name, args,
                workspace_dir=workspace_mod.files_dir(
                    state.get("graph_id") or "graph"),
            )
            out_name = ((node.get("outputs") or [{}])[0]).get("name") or "result"
            records[name] = {out_name: result}
            # Framework inputs must be str — JSON-stringify structured results
            # for downstream LLM nodes; the full value stays in the records.
            merged[out_name] = (
                result if isinstance(result, str)
                else json.dumps(result, ensure_ascii=False, default=str)
            )
            text = json.dumps(result, ensure_ascii=False, default=str)
            infos.append({
                "name": name,
                "status": "completed",
                "output": {out_name: text[:300] + "…" if len(text) > 300 else text},
            })
        except Exception as e:
            infos.append({"name": name, "status": "failed",
                          "output": {"error": str(e)[:500]}})
            state["_source_nodes"] = state.get("_source_nodes", []) + infos
            state["_source_records"] = records
            raise
    state["_source_nodes"] = state.get("_source_nodes", []) + infos
    state["_source_records"] = records
    return merged


def _prepare_ltm(graph_doc: dict, ordered: list[dict], inputs: dict, state: dict) -> tuple[dict, list[dict]]:
    """Open a memory store for every task with use_long_term_memory=true.

    Only opens them. Searching happens when each node actually runs
    (_attach_ltm), because that is the first moment its real inputs exist: a
    node fed by an upstream node is not described by the workflow's inputs at
    all, and searching with those returned whatever happened to be nearest.

    Returns (memories, tasks) — the task list is returned unchanged, so the
    caller's pipeline stays the same shape. Failures are recorded in
    state["memory_error"] and never fail the run.
    """
    graph_id = graph_doc.get("id", "graph")
    memories: dict[str, object] = {}
    for task in ordered:
        if not task.get("use_long_term_memory"):
            continue
        name = task.get("name")
        # A table node keeps rows in SQLite and never searches a corpus.
        # Opening one for it would load FAISS and an embedding model —
        # seconds per node — to build an index nothing reads.
        if memory_policy.policy(task)["kind"] == "table":
            continue
        try:
            with _ltm_lock(f"{graph_id}/{name}"):
                memories[name] = memory_store.open_memory(graph_id, name, create=True)
        except Exception:
            state["memory_error"] = traceback.format_exc()

    # A node may read another node's memory. That store has to be opened too,
    # and read-only: naming it here must not bring a store into existence for a
    # node that does not remember anything.
    for task in ordered:
        if not task.get("use_long_term_memory"):
            continue
        for name in (memory_policy.stores_read_by(task) or []):
            if name in memories:
                continue
            try:
                with _ltm_lock(f"{graph_id}/{name}"):
                    opened = memory_store.open_memory(graph_id, name, create=False)
                if opened is not None:
                    memories[name] = opened
            except Exception:
                state["memory_error"] = traceback.format_exc()
    return memories, ordered


def _agent_for_node(agent_manager, node):
    """The agent that runs a node.

    The framework names an agent after its task rather than identically to it
    ("judge" becomes "JudgeAgent"), so the node's own record of which agent it
    holds is the only reliable way across.
    """
    wanted = next((a.get("name") for a in (node.agents or []) if a.get("name")), None)
    if not wanted:
        return None
    return next((a for a in (agent_manager.agents or []) if a.name == wanted), None)


def _recall_before_running(agent, task: dict, stores: dict, state: dict,
                           graph_id: str = "graph", session: str | None = None) -> None:
    """Make this node search memory at the moment it runs.

    The action's prompt is a template filled with the node's inputs, and those
    inputs are only known once everything upstream has finished. So the recall
    is spliced into the prompt here, for this one call, and taken out again
    afterwards.
    """
    from evoagentx.actions.customize_action import CustomizeAction

    action = next((a for a in (agent.actions or []) if isinstance(a, CustomizeAction)), None)
    if action is None:
        return
    base_prompt = action.prompt
    if base_prompt is None:
        return
    original = action.async_execute

    async def execute_with_recall(*args, inputs=None, **kwargs):
        block = ""
        try:
            if session:
                block = memory_policy.session_block(
                    stm_store.recent(graph_id, session,
                                     memory_policy.policy(task)["session_recall"]),
                    task,
                )
            block += await memory_policy.recall_async(
                stores, task, inputs or {},
                history=lambda agent: memory_store.list_entries(graph_id, agent),
                # Source-node outputs included, so a node dated by `as_of`
                # can be held to it even though it never takes it as an input.
                run_data=state.get("_effective_inputs"),
                table=table_store, graph_id=graph_id,
            )
        except Exception:
            # Memory is an aid, not a precondition: a store that cannot be
            # searched must not stop the node from running.
            state["memory_error"] = traceback.format_exc()
        # The prompt goes through str.format(), so braces in the recalled
        # content (it is often JSON) have to survive as literals.
        action.prompt = base_prompt + block.replace("{", "{{").replace("}", "}}")
        try:
            return await original(*args, inputs=inputs, **kwargs)
        finally:
            action.prompt = base_prompt

    action.async_execute = execute_with_recall


def _keeps_table(task: dict) -> bool:
    """Does this node keep a table rather than a searchable corpus?"""
    return bool(task.get("use_long_term_memory")) and \
        memory_policy.policy(task)["kind"] == "table"


def _attach_ltm(agent_manager, graph, graph_doc: dict, memories: dict, state: dict) -> None:
    """Give each memory-enabled node its store, and its recall.

    Matched through the node rather than by agent name: the two differ, and
    matching on the name silently attached nothing at all.
    """
    task_by_name = {t.get("name"): t for t in graph_doc.get("tasks") or []}
    for node in graph.nodes:
        task = task_by_name.get(node.name, {"name": node.name})
        memory = memories.get(node.name)
        # Gated on the node remembering, not on a store having been opened:
        # a table node has no store and still has to be given its recall.
        if memory is None and not _keeps_table(task):
            continue
        agent = _agent_for_node(agent_manager, node)
        if agent is None:
            state.setdefault("memory_notes", []).append(
                f"No agent found for node '{node.name}'; it will store nothing."
            )
            continue
        if memory is not None:
            agent.use_long_term_memory = True
            agent.storage_handler = memory.storage_handler
            agent.long_term_memory = memory
        _recall_before_running(agent, task_by_name.get(node.name, {"name": node.name}),
                               memories, state,
                               graph_id=graph_doc.get("id", "graph"),
                               session=state.get("session"))


def _save_ltm(graph_doc: dict, memories: dict, graph, wf, state: dict,
              succeeded: bool = True) -> None:
    """Persist what each LTM-enabled node chose to keep.

    Which fields those are is the node's own setting (memory_policy); a node
    that selected nothing present in this run stores nothing at all rather
    than an empty entry that would still occupy a retrieval slot later.
    """
    tasks = graph_doc.get("tasks", []) or []
    # Table memory deliberately has no entry in `memories`: that mapping only
    # contains vector stores.  A graph made entirely of table-backed nodes
    # must still reach the write loop (and its session log).
    if not memories and not any(_keeps_table(task) for task in tasks):
        return
    from evoagentx.core.message import Message, MessageType

    try:
        exec_data = wf.environment.get_all_execution_data() if wf is not None else {}
    except Exception:
        exec_data = {}
    task_by_name = {t.get("name"): t for t in tasks}
    for node in graph.nodes:
        memory = memories.get(node.name)
        task = task_by_name.get(node.name, {"name": node.name})
        if memory is None and not _keeps_table(task):
            continue
        try:
            node_inputs = {
                p.name: exec_data[p.name] for p in (node.inputs or []) if p.name in exec_data
            }
            node_outputs = {
                p.name: exec_data[p.name] for p in (node.outputs or []) if p.name in exec_data
            }
            payload = memory_policy.select(
                task, node_inputs, node_outputs, succeeded=succeeded,
                run_data=exec_data,
            )
            if payload is None:
                continue
            # The same selection, also appended to the session log when the run
            # is part of one: what to keep is the node's decision, and it does
            # not change with how long it is kept for.
            if state.get("session"):
                stm_store.append(graph_doc.get("id", "graph"), state["session"], {
                    "node": node.name,
                    "run_id": state.get("run_id"),
                    "at": _utcnow(),
                    "inputs": payload.get("inputs") or {},
                    "outputs": payload.get("outputs") or {},
                })
            def as_message(body):
                return Message(
                    content=json.dumps(body, ensure_ascii=False, default=str),
                    msg_type=MessageType.RESPONSE,
                    agent=node.name,
                    wf_goal=graph_doc.get("goal", ""),
                    wf_task=node.name,
                    wf_task_desc=task.get("description", ""),
                )

            graph_id = graph_doc.get("id", "graph")
            # A table node writes a row and is done: the primary key does what
            # a read-modify-write under a lock used to do, and badly.
            row = memory_policy.table_write(task, payload)
            if row is not None:
                table_store.upsert(graph_id, node.name, row["subject"],
                                   row["at"], row["payload"], _utcnow())
                state.setdefault("memory_written", []).append(
                    {"node": node.name, "kind": "table",
                     "subject": row["subject"], "at": row["at"]})
                continue
            # A table node without its configured subject cannot produce a
            # row.  It must not silently fall through and create a vector
            # corpus, since the two storage kinds have different semantics.
            if _keeps_table(task):
                state.setdefault("memory_notes", []).append(
                    f"Node '{node.name}' produced no table-memory subject; nothing was stored."
                )
                continue
            with _ltm_lock(f"{graph_id}/{node.name}"):
                # Re-opened inside the lock rather than written through the
                # copy this run has been holding since it started. `save()`
                # writes the whole corpus, and that copy was loaded minutes
                # ago — before the other items of a batch wrote theirs. Adding
                # to it and saving would put the store back as it was and lose
                # every entry written in between.
                fresh = memory_store.open_memory(graph_id, node.name, create=True)
                _write_entry(fresh, graph_id, node.name, task, payload, as_message)
                fresh.save()
        except Exception:
            state["memory_error"] = traceback.format_exc()


def _write_entry(memory, graph_id: str, node_name: str, task: dict,
                 payload: dict, as_message) -> None:
    """Store this run's selection under the subject it is about.

    A node that tracks a subject keeps one entry per subject — the obligor's
    whole history — rather than one per run. A run is a dated state folded
    into that entry, so "what has this company done over time" is a thing you
    can open, not something to be reassembled from scattered days.

    Must be called with the node's memory lock held: this is a
    read-modify-write of an entry other records of the same batch also touch.
    """
    subject = memory_policy.subject_of(task, payload)
    if subject is None:
        memory.add([as_message(payload)])          # nothing to accumulate against
        return

    dated_by = memory_policy.policy(task)["at"]
    mine, ids = [], []
    for entry in memory_store.list_entries(graph_id, node_name):
        body = memory_policy._unwrap(entry.get("content"))
        if not isinstance(body, dict):
            continue
        if memory_policy.subject_of(task, body) != subject:
            continue
        mine.append(body)
        if entry.get("memory_id"):
            ids.append(entry["memory_id"])

    merged = memory_policy.merge_timeline(
        mine, memory_policy.as_timeline(task, payload), dated_by)
    memory.add([as_message(merged)])
    if ids:
        # After the merged entry is in, never before: a failure here leaves a
        # duplicate, which recall folds back together by date, where the other
        # order would lose the history outright.
        try:
            memory.delete(ids)
        except Exception:
            pass


def _snapshot_nodes(state: dict, graph, wf, succeeded: bool) -> None:
    """Capture final node statuses and per-node outputs.

    On success the framework resets node statuses to pending, so completed
    nodes are reported as completed based on the run outcome; on failure the
    live statuses are kept and any node left running is marked failed.
    Outputs are read from the Environment execution data by output name.
    """
    exec_data = {}
    if wf is not None:
        try:
            exec_data = wf.environment.get_all_execution_data()
        except Exception:
            exec_data = {}
    nodes = []
    for node in graph.nodes:
        status = node.status.value
        if succeeded:
            status = "completed"
        elif status == "running":
            status = "failed"
        outputs = {
            out.name: exec_data[out.name]
            for out in (node.outputs or [])
            if out.name in exec_data
        }
        nodes.append({"name": node.name, "status": status, "output": outputs or None})
    state["nodes"] = state.get("_source_nodes", []) + nodes


def mark_interrupted() -> int:
    """Flag persisted runs left mid-flight by a previous process.

    Execution cannot be resumed — the work was a thread making LLM calls, and
    re-running it would bill the user twice for a result they may already have
    paid for. What matters is that the run stops claiming to be running, so it
    reads as "interrupted by a restart" instead of hanging forever.
    """
    if not RUNS_DIR.is_dir():
        return 0
    marked = 0
    for path in RUNS_DIR.glob("*.json"):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("status") not in ("running", "pending"):
            continue
        data["status"] = "failed"
        data["error"] = (
            "Interrupted: the server restarted while this run was in flight. "
            "Nothing was resumed — start it again if you still need the result."
        )
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False, default=str)
            marked += 1
        except OSError:
            continue
    return marked


def _persist_run(state: dict) -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    data = {k: v for k, v in state.items() if not k.startswith("_")}
    try:
        with open(RUNS_DIR / f"{state['run_id']}.json", "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    except OSError:
        pass


def _public(state: dict) -> dict:
    """Run state for the API: live node statuses while running, public keys."""
    out = {k: v for k, v in state.items() if not k.startswith("_")}
    graph = state.get("_graph")
    if out["status"] == "running" and graph is not None:
        live = {node.name: node.status.value for node in graph.nodes}
        out["nodes"] = [
            {**n, "status": live.get(n["name"], n["status"])}
            for n in out["nodes"]
        ]
    return out


def apply_review_outcome(run_id: str, outcome: dict) -> None:
    """Update a live (in-memory) run with a resolved review's outcome."""
    with _lock:
        state = _runs.get(run_id)
        if state is None:
            return
        state.update(outcome)
        _persist_run(state)


def _read_run(run_id: str) -> dict | None:
    """A run as persisted on disk, or None."""
    path = RUNS_DIR / f"{run_id}.json"
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def get_run(run_id: str) -> dict | None:
    with _lock:
        state = _runs.get(run_id)
        if state is not None:
            return _public(state)
    return _read_run(run_id)


def list_runs(graph_id: str | None = None) -> list[dict]:
    """Recent runs (newest first, cap 20), merging in-memory and persisted runs."""
    by_id: dict[str, dict] = {}
    if RUNS_DIR.is_dir():
        for path in RUNS_DIR.glob("*.json"):
            try:
                with open(path, encoding="utf-8") as f:
                    by_id[path.stem] = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
    with _lock:
        for run_id, state in _runs.items():
            by_id[run_id] = _public(state)
    runs = list(by_id.values())
    if graph_id:
        runs = [r for r in runs if r.get("graph_id") == graph_id]
    runs.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return runs[:20]
