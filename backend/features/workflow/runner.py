"""Run engine for EvoAgentX Studio.

Runs execute in daemon threads; state lives in a module-level dict and is
persisted to studio-data/runs/{run_id}.json on completion. Node status is read
live off the executing WorkFlowGraph; node outputs are captured after the run
from the workflow Environment's execution data (best effort per output name).
"""

import asyncio
import copy
import json
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone

from backend.features import model_bridge
from backend.features.execution import token_usage
from backend.features.persistence import write_json
from backend.features.chat import chat_control
from backend.features.memory.identity import execution_snapshot
from backend.features.memory.bindings import runtime_context
from backend.api import memory_policy
from backend.api import memory_store
from backend.api import table_store
from backend.api import stm_store
from backend.api import review as review_mod
from backend.api import sources
from backend.api import tools_registry
from backend.api import workspace as workspace_mod
from backend.api.graphs import (is_source_task, is_tool_task, parked_task_names,
                    strip_task, subgraph_from, topo_sort_tasks)
from backend.api import skills_api
from backend.api.studio_config import data_path

RUNS_DIR = data_path("runs")

_runs: dict[str, dict] = {}
_lock = threading.Lock()

# Stop requests for runs that have not registered themselves yet, as
# {run_id: monotonic time}. A batch item and an Evolve candidate both publish
# the run id they are about to use before `start_run` binds it; a stop landing
# in that window found nothing to cancel, said so, and the record then ran in
# full — on the user's money, while the batch reported it as interrupted.
_pending_cancels: dict[str, float] = {}
_PENDING_CANCEL_TTL = 600.0

# How long a stop waits for the run's own loop to settle the run before saying
# so itself. A cancelled task lands in milliseconds; a run still executing
# after this is inside something with no interruption point — a tool call, a
# request already with the provider — and must stop claiming to be in progress.
_CANCEL_GRACE_SECONDS = 2.0


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_llm(usage_key: str | None = None):
    """The one place a run's model is built: the `llm` package, via the bridge.

    Every call the returned model makes — and every call an agent the
    framework clones from its config makes — goes through the package and
    reports its usage to `usage_key`. Tests replace this seam wholesale.
    """
    return model_bridge.workflow_model(usage_key=usage_key)


def _model_for_run(state):
    from backend.features.execution.provider_batch import validate
    validate({}, state.get('llm_batch_size'))
    return _make_llm(usage_key=token_usage.usage_key(state))



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
              session: str | None = None, record_index: int | None = None,
              plan_id: str | None = None, graph_revision: str | None = None,
              batch_id: str | None = None, session_started_at: str | None = None,
              llm_batch_size: int | None = None, gate=None) -> str:
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
    record_index: which of the source's records this run takes. A source that
    yields several used to hand over the first one without saying so.
    """
    graph = copy.deepcopy(graph)
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
        "execution_snapshot": {"graph": copy.deepcopy(graph), "llm_batch_size": llm_batch_size,
                               "session": session, "record_index": record_index},
        "review_status": None,
        "created_at": _utcnow(),
        "session": (session or "").strip() or None,
        "record_index": record_index,
        # How this run was made: the plan it was confirmed from, the graph
        # revision that plan described, and a glance at what went in.
        "plan_id": plan_id,
        "graph_revision": graph_revision,
        "batch_id": batch_id,
        "llm_batch_size": llm_batch_size,
        # When the thing the user started began: a batch's records all share
        # their batch's start, a single run its own. Names the run's folder.
        "session_started_at": session_started_at,
        "input_summary": _summarise_inputs(inputs),
        "_executing": True,
        # Whose files the tools work on. A candidate replay during Evolve runs
        # under its own throwaway id, on a copy of the real workflow's files.
        "_workspace_id": graph.get("_workspace_id") or graph.get("id"),
        "_graph": None,  # live WorkFlowGraph, set once execution starts
        "_gray_zone": gray_zone,
        # The chunk this run keeps step with, when its batch runs node by node.
        "_gate": gate,
    }
    with _lock:
        # A stop that arrived for this id before the run existed applies to
        # it: the walker raises at its first node, so no model is called.
        if _pending_cancels.pop(run_id, None) is not None:
            state["_cancel_requested"] = True
        _runs[run_id] = state
    # Written before execution starts, not only when it ends: a run that the
    # process never finishes (a restart, a crash) would otherwise vanish, and
    # the client polling it gets a 404 for something it just started.
    if not _persist_run(state):
        with _lock:
            _runs.pop(run_id, None)
        raise OSError("Could not persist Run; execution was not started.")
    if background:
        thread = threading.Thread(
            target=_execute_run_scoped, args=(run_id, graph, inputs or {}), daemon=True
        )
        thread.start()
    else:
        _execute_run_scoped(run_id, graph, inputs or {})
    return run_id


def _execute_run_scoped(run_id: str, graph_doc: dict, inputs: dict) -> None:
    """One run, with its own custom-tool scope: a class toolkit keeps one
    instance (one worker) for the whole run, and it ends with the run.

    The run also gets its own cancellation control, in effect for everything
    it starts: every Studio worker process (an evaluator's Python, a Dataset
    read, a preprocessing tool) watches the one in context and is killed with
    its group when a stop sets it. Without that, `cancel_run` could stop the
    node loop but not the subprocess the loop was waiting for.
    """
    from backend.features.library import tool_sessions
    # A run started inside something that already has a stop of its own — an
    # assistant turn, a sample — keeps that one: the run is that request's
    # work, and one Stop has to cover both. A batch item's thread carries no
    # such control, so each item gets its own.
    outer = chat_control.current.get()
    control = outer or chat_control.Control()
    with _lock:
        state = _runs.get(run_id) or {}
        state["_control"] = control
        # A stop that arrived before this run started belongs to this run: the
        # node loop raises at its first node either way, and a borrowed
        # control must not be set on the request's behalf.
        if state.get("_cancel_requested") and outer is None:
            control.event.set()
    token = chat_control.current.set(control)
    try:
        with tool_sessions.scope() as scope:
            with _lock:
                state["_tool_scope"] = scope
            _execute_run(run_id, graph_doc, inputs)
    finally:
        chat_control.current.reset(token)
        with _lock:
            state.pop("_tool_scope", None)
            state.pop("_control", None)


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


def cancel_run(run_id: str, *, expected: bool = False) -> dict:
    """Stop a run that is executing — actually stop it.

    The engine walks the nodes on a loop this can reach: the in-flight model
    call is cancelled, the run is marked `cancelled`, and what it had
    produced so far stays readable. A run that has already settled is left
    as it is and says so.

    `expected`: the caller assigned this run id and knows it is about to be
    used, so an id this module has not seen yet is a run that has not started
    rather than one that does not exist. The stop is held for it and applied
    the moment it starts, which is the difference between reporting a record
    as interrupted and it actually being interrupted.
    """
    with _lock:
        state = _runs.get(run_id)
    if state is None:
        persisted = _read_run(run_id) if "_read_run" in globals() else None
        if expected and persisted is None:
            now = time.monotonic()
            with _lock:
                for stale in [k for k, at in _pending_cancels.items()
                              if now - at > _PENDING_CANCEL_TTL]:
                    del _pending_cancels[stale]
                _pending_cancels[run_id] = now
            return {"cancelled": True, "before_start": True}
        return {"cancelled": False,
                "reason": f"run already {persisted['status']}" if persisted else "no such run"}
    if state.get("status") != "running":
        return {"cancelled": False, "reason": f"run already {state.get('status')}"}
    state["_cancel_requested"] = True
    cancel = state.get("_cancel")
    if cancel is not None:
        try:
            cancel()
        except RuntimeError:
            # The run settled between the check above and here.
            pass
    # Everything this run started, not only its node loop: the worker holding
    # a tool (and whatever that tool started) is killed with its group, and
    # any other worker process watching the run's control ends itself.
    control = state.get("_control")
    if control is not None:
        control.event.set()
    scope = state.get("_tool_scope")
    if scope is not None:
        try:
            scope.kill()
        except Exception:
            pass
    # A tool call already in flight has no interruption point, so the loop may
    # not come back for a while. The run says it stopped either way.
    timer = threading.Timer(_CANCEL_GRACE_SECONDS, _settle_uninterruptible, args=(run_id,))
    timer.daemon = True
    timer.start()
    return {"cancelled": True}


def _settle_uninterruptible(run_id: str) -> None:
    """Report a stopped run as stopped although its thread is still in there.

    The thread is inside a call that cannot be interrupted. It goes on to
    finish, as an abandoned run does, and whatever verdict it reaches is kept
    beside — not instead of — what the user was told.
    """
    with _lock:
        state = _runs.get(run_id)
        if (state is None or not state.get("_cancel_requested")
                or state.get("status") != "running" or not state.get("_executing")):
            return
        state["_abandoned"] = True
        state["status"] = "cancelled"
        state["error"] = ("Stopped by the user. Part of this run could not be interrupted "
                          "— a tool call, or a request already with the provider — so that "
                          "part finishes in the background and may still be billed.")
        for node in state.get("nodes") or []:
            if node.get("status") == "running":
                node["status"] = "cancelled"
            elif node.get("status") == "pending":
                node["status"] = "skipped"
        snapshot = dict(state)
    _persist_run(snapshot)


def _stopped(state: dict) -> None:
    """Record a run the user stopped: what was mid-flight is cancelled, what
    had not started is skipped, and nothing is written to memory."""
    _set_status(state, "cancelled", error="Stopped by the user before it finished.",
                node_error=None, debug_error=None)
    for node in state.get("nodes") or []:
        if node.get("status") == "running":
            node["status"] = "cancelled"
        elif node.get("status") == "pending":
            node["status"] = "skipped"


def _cancel_on(loop, task) -> None:
    """Cancel `task` from another thread; a loop that already closed means
    the run has settled, and there is nothing left to stop."""
    if loop.is_closed():
        return
    try:
        loop.call_soon_threadsafe(task.cancel)
    except RuntimeError:
        pass


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


class NodeError(Exception):
    """A failure pinned to the node and, where it applies, the field.

    What a person needs first is which node stopped and why, in a sentence;
    the traceback is kept, separately, for when the sentence is not enough.
    """

    def __init__(self, code: str, node: str, message: str, field: str | None = None,
                 retryable: bool = False):
        super().__init__(message)
        self.code, self.node, self.field, self.message = code, node, field, message
        # True when running the same record again is the fix — the model was
        # unreachable, not wrong. A batch re-queues those; the drawer says so.
        self.retryable = retryable

    def as_dict(self) -> dict:
        out = {"code": self.code, "node": self.node, "message": self.message}
        if self.field:
            out["field"] = self.field
        if self.retryable:
            out["retryable"] = True
        return out


def _unreachable(exc: BaseException) -> str | None:
    """The message of the transient failure in the chain, else None.

    The framework wraps model errors in RuntimeError as they pass through
    agents and actions; whether the model was only unavailable — a timeout,
    an overload, a rate limit — is decided on the whole chain by the bridge,
    which already tried the call again before giving up.
    """
    from backend.features.model_bridge import is_transient
    if not is_transient(exc):
        return None
    seen = 0
    while exc is not None and seen < 10:
        cause = exc.__cause__ or exc.__context__
        if cause is None:
            return str(exc)
        exc, seen = cause, seen + 1
    return str(exc)


def _summarise_inputs(inputs: dict, width: int = 80) -> dict:
    """Enough of each input to recognise the run in a list, never the payload."""
    out = {}
    for key, value in (inputs or {}).items():
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        out[key] = text if len(text) <= width else text[:width] + "…"
    return out


class _ExecEnv:
    """Stands in for the framework's Environment for the code that reads one.

    Memory and run artifacts ask `wf.environment.get_all_execution_data()`
    for everything a run produced; the engine keeps that as one flat dict.
    """

    def __init__(self):
        self.data: dict = {}
        self.environment = self

    def get_all_execution_data(self) -> dict:
        return dict(self.data)


class _Nodes:
    """The framework nodes of this run's LLM tasks, for attaching memory."""

    def __init__(self, nodes):
        self.nodes = list(nodes)


async def execute_llm_node(agent, task: dict, inputs: dict, state: dict) -> dict:
    """Run one LLM node through its agent and return its declared outputs.

    Module-level and async so a test can stand in a recorder for it: the
    fixed end-to-end flows assert what every node was handed, without a
    model. The recall hook wraps the action underneath, so memory still
    reaches the prompt on the way through.
    """
    from evoagentx.core.message import MessageType

    if (task.get('harness') or {}).get('engine') == 'deepagents':
        from backend.api.harness import run_workflow_node
        block = ''
        if task.get('use_long_term_memory'):
            try:
                block = await memory_policy.recall_async(
                    state.get('_harness_memories', {}), task, inputs,
                    history=lambda name: memory_store.list_entries(state.get('graph_id', 'graph'), name),
                    run_data=runtime_context(state), table=table_store,
                    graph_id=state.get('graph_id', 'graph'))
                if state.get('session') and memory_policy.policy(task)['read_enabled']:
                    block = memory_policy.session_block(stm_store.recent(state['graph_id'], state['session'], memory_policy.policy(task)['session_recall']), task) + block
            except Exception:
                state['memory_error'] = traceback.format_exc()
        return await run_workflow_node(task, inputs, state, block)

    message = await agent.async_execute(
        action_name=agent.customize_action_name,
        action_input_data=inputs,
        return_msg_type=MessageType.RESPONSE,
        wf_goal=state.get("_goal", ""),
        wf_task=task.get("name"),
        wf_task_desc=task.get("description", ""),
    )
    content = message.content
    if hasattr(content, "get_structured_data"):
        data = content.get_structured_data() or {}
    elif isinstance(content, dict):
        data = content
    else:
        data = {}
    return {o.get("name"): data[o.get("name")]
            for o in (task.get("outputs") or []) if o.get("name") in data}


def _as_prompt_value(value):
    """Framework inputs must be text; structured values go in as JSON."""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def _gather(task: dict, bindings: dict, values: dict, external: dict) -> dict:
    """A node's inputs: from the edges that feed it, else from the run's own.

    Nothing else. A field of the same name on an unconnected node is not a
    source of data, whatever the framework used to infer.
    """
    found, missing = {}, []
    for p in task.get("inputs") or []:
        name = p.get("name")
        bound = bindings.get(name)
        if bound is not None and bound["field"] in values.get(bound["node"], {}):
            found[name] = values[bound["node"]][bound["field"]]
        elif name in external:
            found[name] = external[name]
        elif p.get("required", True):
            missing.append(name)
    if missing:
        raise NodeError("missing_input", task.get("name"),
                        f"Node '{task.get('name')}' needs {missing[0]!r}"
                        + (f" (and {len(missing) - 1} more)" if len(missing) > 1 else "")
                        + ": nothing feeds it and it was not supplied.",
                        field=missing[0])
    return found


def _tool_outputs(task: dict, result) -> dict:
    """Spread a tool's return over its declared outputs, by key when several."""
    names = [o.get("name") for o in (task.get("outputs") or []) if o.get("name")]
    from backend.api import custom_tools
    custom = custom_tools.find(task.get('tool'))
    if custom and custom[0].get('factory'):
        return result
    if len(names) <= 1:
        return {(names[0] if names else "result"): result}
    if not isinstance(result, dict):
        raise NodeError("tool_failed", task.get("name"),
                        f"Tool node '{task.get('name')}' declares outputs {names} but "
                        f"returned a {type(result).__name__}, not an object with those keys.")
    absent = [n for n in names if n not in result]
    if absent:
        raise NodeError("tool_failed", task.get("name"),
                        f"Tool node '{task.get('name')}' returned no {absent}; it has "
                        f"{sorted(result)}.", field=absent[0])
    return {n: result[n] for n in names}


def _clip(value, width: int = 300):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text[:width] + "…" if len(text) > width else text


async def _walk(plan: dict, graph_doc: dict, inputs: dict, state: dict,
                agents: dict, env: _ExecEnv) -> dict:
    """Run the plan's nodes in order, each fed only by its edges.

    Serial on purpose — the first version of a new execution order is the
    one that has to be right, not fast. Returns the outputs of the leaf
    nodes, which is the run's result.
    """
    by_name = {t.get("name"): t for t in graph_doc.get("tasks") or []}
    values: dict[str, dict] = {}
    external = dict(inputs or {})
    consumed = {b["node"] for n in plan["nodes"] for b in n["input_bindings"].values()}
    status = {n["name"]: n for n in state["nodes"]}
    env.data.update(external)
    state["_source_records"] = {}
    # A batch run node by node: the records of one chunk keep step, so none
    # starts a node before the others have finished the one before it.
    gate = state.get("_gate")
    if gate is not None:
        gate.join(state["run_id"])
    try:
        return await _walk_nodes(plan, by_name, values, external, consumed, status,
                                 graph_doc, state, agents, env, gate)
    finally:
        if gate is not None:
            gate.leave(state["run_id"])


async def _walk_nodes(plan, by_name, values, external, consumed, status,
                      graph_doc, state, agents, env, gate) -> dict:
    for position, node in enumerate(plan["nodes"]):
        name = node["name"]
        task = by_name[name]
        if state.get("_cancel_requested"):
            raise asyncio.CancelledError()
        took_slot = False
        if gate is not None:
            took_slot = await asyncio.to_thread(
                gate.wait_turn, state["run_id"], position,
                lambda: bool(state.get("_cancel_requested")), node["kind"] == "llm")
            if state.get("_cancel_requested"):
                if took_slot:
                    gate.release_slot()
                raise asyncio.CancelledError()
        status[name]["status"] = "running"
        # Model calls report their usage to the node running now (the walk
        # is serial), as well as to the run, the moment each one returns.
        state["_usage_node"] = status[name]
        try:
            if node["kind"] == "source":
                # A record handed in — a batch item, a re-run of a known
                # sample — is the record. The source samples only for what is
                # not supplied; re-sampling over it handed every record of a
                # batch the same first sample, whatever its inputs said.
                declared = [o.get("name") for o in (task.get("outputs") or []) if o.get("name")]
                supplied = {k: external[k] for k in declared if k in external}
                if state.get('batch_id'):
                    missing = [o['name'] for o in task.get('outputs', []) if o.get('required', True) and o['name'] not in supplied]
                    if missing:
                        raise NodeError('source_failed', name, f"Prepared batch record is missing source fields: {missing}. The source was not reread.")
                    out = {key: supplied.get(key) for key in declared}
                elif declared and len(supplied) == len(declared):
                    out = supplied
                else:
                    try:
                        from backend.features.data.input_composition import is_reference, read_record
                        index = 0 if is_reference(task) else (state.get("record_index") or 0)
                        if (task.get('source') or {}).get('type') == 'dataloader' and not is_reference(task):
                            record = read_record(graph_doc, task, index, external,
                                                 lambda: state.get('_cancel_requested', False))
                        else:
                            record = sources.records_from_source_node(task)[index]
                        out = {**dict(record), **supplied}
                    except NodeError:
                        raise
                    except Exception as e:
                        raise NodeError("source_failed", name,
                                        f"Source '{name}' could not produce a record: {e}") from e
                state["_source_records"][name] = out
            elif node["kind"] == "tool":
                args = _gather(task, node["input_bindings"], values, external)
                try:
                    out = _tool_outputs(task, tools_registry.call_tool(
                        task.get("tool"), args,
                        workspace_dir=workspace_mod.files_dir(
                            state.get("_workspace_id") or state.get("graph_id") or "graph")))
                except NodeError:
                    raise
                except Exception as e:
                    raise NodeError("tool_failed", name,
                                    f"Tool node '{name}' ({task.get('tool')}) failed: {e}") from e
                state["_source_records"][name] = out
            else:
                args = _gather(task, node["input_bindings"], values, external)
                agent = agents.get(name)
                if agent is None:
                    raise NodeError("plan_invalid", name, f"No agent was built for node '{name}'.")
                try:
                    prompt_inputs = {k: _as_prompt_value(v) for k, v in args.items()}
                    out = await execute_llm_node(agent, task, prompt_inputs, state)
                except NodeError:
                    raise
                except Exception as e:
                    if _unreachable(e):
                        raise NodeError(
                            "llm_unreachable", name,
                            f"Node '{name}' could not reach the model: {_unreachable(e)}",
                            retryable=True) from e
                    raise NodeError("llm_failed", name,
                                    f"Node '{name}' failed while calling the model: {e}") from e
            values[name] = out
            state.setdefault('node_outputs', {})[name] = out
            if node['kind'] == 'source':
                state['inputs'] = {**state.get('inputs', {}), **out}
            env.data.update(out)
            state.setdefault("_node_io", {})[name] = {
                "inputs": args if node["kind"] != "source" else {},
                "output": out,
            }
            # What memory and the artifacts see; source fields are context
            # (the subject, the date) even though they only flow along edges.
            state["_effective_inputs"] = dict(env.data)
            status[name]["status"] = "completed"
            status[name]["output"] = {k: _clip(v) for k, v in out.items()} or None
            if gate is not None:
                gate.done(state["run_id"], position)
            if (status[name].get("token_usage") or {}).get("reported_calls"):
                # A checkpoint of what has been spent, should the process die
                # before the run settles; the live endpoints read memory.
                _persist_run(state)
        except Exception as e:
            status[name]["status"] = "failed"
            status[name]["output"] = {"error": (getattr(e, "message", None) or str(e))[:500]}
            raise
        finally:
            state.pop("_usage_node", None)
            if took_slot:
                gate.release_slot()

    result = {}
    for node in plan["nodes"]:
        if node['kind'] == 'source':
            continue
        if node["name"] not in consumed:
            result.update(values.get(node["name"], {}))
    return result


@execution_snapshot
def _execute_run(run_id: str, graph_doc: dict, inputs: dict) -> None:
    """Run a graph as the plan describes it: one DAG, every kind of node.

    The framework is used for what it is good at — building an agent for an
    LLM task and running its action — and no longer for deciding order or
    data flow: each LLM node gets its own one-node framework graph, so the
    framework cannot infer edges the canvas does not draw, and the walker
    above hands each node exactly what its edges feed it.
    """
    from evoagentx.agents.agent_manager import AgentManager
    from evoagentx.workflow.workflow_graph import SequentialWorkFlowGraph
    from backend.api import run_plan

    state = _runs[run_id]
    graph = None
    env = _ExecEnv()
    memories: dict = {}
    try:
        plan = run_plan.compile_plan(graph_doc)
        state["_goal"] = graph_doc.get("goal", "")
        state["_effective_inputs"] = dict(inputs or {})
        state["nodes"] = (
            [{"name": n["name"], "status": "pending", "output": None} for n in plan["nodes"]]
            + [{"name": s, "status": "skipped",
                "output": {"note": "not part of this run — disabled, unwired, or before the start"}}
               for s in plan["skipped"]])
        by_name = {t.get("name"): t for t in graph_doc.get("tasks") or []}
        llm_tasks = [by_name[n["name"]] for n in plan["nodes"] if n["kind"] == "llm"]

        memories, llm_tasks = _prepare_ltm(graph_doc, llm_tasks, inputs, state)
        state["_harness_memories"] = memories
        agents: dict = {}
        framework_nodes = []
        if llm_tasks:
            llm = _model_for_run(state)
            agent_manager = AgentManager()
            tools = tools_registry.resolve_tools(
                sorted({n for t in llm_tasks for n in (t.get("tool_names") or [])}),
                workspace_dir=workspace_mod.files_dir(
                    graph_doc.get("_workspace_id") or graph_doc.get("id", "graph")),
            ) or None
            # Skills are standing instructions, not tools: resolve them into
            # each node's system prompt so the framework runs an ordinary task.
            for task in [strip_task(t) for t in skills_api.inject_into_tasks(llm_tasks)]:
                one = SequentialWorkFlowGraph(goal=graph_doc.get("goal", ""), tasks=[task])
                agent_manager.add_agents_from_workflow(one, llm_config=llm.config, tools=tools)
                node = one.nodes[0]
                framework_nodes.append(node)
                agents[node.name] = _agent_for_node(agent_manager, node)
                token_usage.attach_usage(agents[node.name].llm, state.get('_usage_key'))
                if state.get('llm_batch_size'):
                    from backend.features.execution.provider_batch import attach_workflow_model
                    attach_workflow_model(agents[node.name].llm, state)
            graph = _Nodes(framework_nodes)
            state["_graph"] = graph
            _attach_ltm(agent_manager, graph, graph_doc, memories, state)

        # A loop of our own rather than asyncio.run(): a stop request from
        # another thread cancels the task, and the model call it is inside
        # is abandoned there and then instead of finishing on our money.
        loop = asyncio.new_event_loop()
        try:
            task = loop.create_task(_walk(plan, graph_doc, inputs, state, agents, env))
            state["_cancel"] = lambda: _cancel_on(loop, task)
            if state.get("_cancel_requested"):
                task.cancel()
            result = loop.run_until_complete(task)
        finally:
            state.pop("_cancel", None)
            loop.close()
        # A run does not evaluate itself: evaluation is asked for, in
        # Evaluate & Evolve or on a result, over saved runs.
        if state.get('_cancel_requested'):
            raise asyncio.CancelledError()
        _set_status(state, "success", result=result)
        if graph is not None:
            _save_ltm(graph_doc, memories, graph, env, state)
        try:
            review = review_mod.maybe_create_review(state, gray_zone=state.get("_gray_zone"),
                                                    rule=graph_doc.get("review"))
            if review:
                state["review_status"] = "awaiting_review"
                state["review_id"] = review["review_id"]
        except Exception:
            state["memory_error"] = traceback.format_exc()
    except (asyncio.CancelledError, chat_control.Cancelled):
        # Stopped on request — the loop, or a worker of the run's that the
        # stop ended. The node mid-flight is marked so, the ones after it
        # never started; nothing is written to memory.
        _stopped(state)
    except Exception as e:
        # A failure while a stop is pending is that stop: what the node was
        # waiting on — a tool's worker, a Dataset reader — was killed by it.
        # A stopped run is not a failed run, and it remembers nothing either.
        if state.get("_cancel_requested"):
            _stopped(state)
        else:
            # The sentence first, the traceback beside it — not instead of it.
            if isinstance(e, NodeError):
                failure = dict(error=e.message, node_error=e.as_dict(), debug_error=traceback.format_exc())
            else:
                failure = dict(error=f"{type(e).__name__}: {e}"[:500], node_error=None,
                               debug_error=traceback.format_exc())
            for n in state.get("nodes") or []:
                if n.get("status") == "running":
                    n["status"] = "failed"
            _set_status(state, "failed", **failure)
            if graph is not None:
                # A node set to remember failed runs too: what did complete has
                # real outputs, and what went wrong is often the thing to keep.
                _save_ltm(graph_doc, memories, graph, env, state, succeeded=False)
    finally:
        token_usage.release_usage(state)
        # Every store this run opened, closed now rather than whenever the
        # garbage collector gets to them: a batch opens them per record.
        for opened in (state.get("_harness_memories") or {}).values():
            memory_store.close_memory(opened)
        persisted = _persist_run(state)
        if not persisted:
            state["persistence_error"] = "Run completed in memory but could not be saved. Restore disk access before recovery."
        state["_executing"] = False
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
        if persisted:
            # Only once it is on disk: get_run falls back to the file.
            _release(run_id, state)


def _release(run_id: str, state: dict) -> None:
    """Forget a settled run: it is on disk, and get_run reads it from there.

    The live state holds the graph, the framework agents and the memory
    handles; kept for every run, a long batch holds all of them at once. A
    run abandoned but still executing stays until its thread ends here.
    """
    with _lock:
        if state.get("status") == "running" and not state.get("_abandoned"):
            return
        for key in [k for k in state if k.startswith("_")]:
            state.pop(key, None)
        if _runs.get(run_id) is state:
            _runs.pop(run_id, None)


def _run_source_nodes(source_nodes: list[dict], inputs: dict, state: dict,
                      record_index: int | None = None) -> dict:
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
            found = sources.records_from_source_node(node)
            from backend.features.data.input_composition import is_reference
            record = found[0 if is_reference(node) else (record_index or 0)]
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
                    state.get("_workspace_id") or state.get("graph_id") or "graph"),
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
        if (task.get("memory") or {}).get("provider") == "mem0":
            from backend.api.mem0_service import Mem0Memory
            memories[name] = Mem0Memory(graph_doc, task, state)
            continue
        # A table node keeps rows in SQLite and never searches a corpus.
        # Opening one for it would load FAISS and an embedding model —
        # seconds per node — to build an index nothing reads.
        if not memory_policy.policy(task)["write_enabled"] or memory_policy.policy(task)["kind"] == "table":
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
        if not task.get("use_long_term_memory") or not memory_policy.policy(task)["read_enabled"]:
            continue
        if (task.get("memory") or {}).get("provider") == "mem0":
            continue
        for name in (memory_policy.stores_read_by(task) if memory_policy.stores_read_by(task) is not None else [task.get("name")]):
            source_task = next((t for t in graph_doc.get("tasks", []) if t.get("name") == name), {})
            if memory_policy.policy(source_task)["kind"] == "table":
                continue
            if (source_task.get("memory") or {}).get("provider") == "mem0":
                from backend.api.mem0_service import Mem0Memory
                memories[name] = Mem0Memory(graph_doc, source_task, state)
                continue
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
            if session and memory_policy.policy(task)["read_enabled"]:
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
                run_data=runtime_context(state),
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
        if not task.get("use_long_term_memory"):
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
        if not task.get("use_long_term_memory") or not memory_policy.policy(task)["write_enabled"]:
            continue
        if memory is None and not _keeps_table(task):
            continue
        try:
            node_inputs = {
                p.name: exec_data[p.name] for p in (node.inputs or []) if p.name in exec_data
            }
            node_outputs = {
                p.name: exec_data[p.name] for p in (node.outputs or []) if p.name in exec_data
            }
            io = state.get("_node_io", {}).get(node.name)
            if io is not None:
                node_inputs, node_outputs = io.get("inputs") or {}, io.get("output") or {}
            memory_context = runtime_context(state, exec_data)
            resolved_context = memory_policy.with_context(task, node_inputs, memory_context)
            missing_context = [field for field in memory_policy.policy(task)["context"] if field not in resolved_context]
            if missing_context:
                state.setdefault("memory_notes", []).append(
                    f"Memory '{node.name}': optional recorded fields unavailable: {', '.join(missing_context)}. Other content is retained.")
            payload = memory_policy.select(
                task, node_inputs, node_outputs, succeeded=succeeded,
                run_data=memory_context,
            )
            if payload is None:
                continue
            for gap in payload.get("unbound") or []:
                what = ("entity" if gap["label"] == "subject" else "time")
                reads = ("reads matched on an entity" if gap["label"] == "subject"
                         else "time-filtered reads")
                state.setdefault("memory_notes", []).append(
                    f"Memory '{node.name}': this run had no '{gap['reference']}' ({what}); "
                    f"the record was kept without it and is left out of {reads}.")
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
                settings = memory_policy.policy(task)
                table_store.write(graph_id, node.name, row["subject"],
                                  row["at"], row["payload"], _utcnow(),
                                  mode=settings["write_mode"], key=row.get("key"),
                                  execution_id=state.get("run_id"))
                state.setdefault("memory_written", []).append(
                    {"node": node.name, "kind": "table",
                     "subject": row["subject"], "at": row["at"]})
                continue
            # A table node without its configured subject cannot produce a
            # row.  It must not silently fall through and create a vector
            # corpus, since the two storage kinds have different semantics.
            if _keeps_table(task):
                state.setdefault("memory_notes", []).append(
                    f"Memory '{node.name}': this run had no '{memory_policy.policy(task)['match']}', and this "
                    f"memory updates one record per entity and date, so nothing was stored. "
                    f"Switch Write mode to Append to keep records without an entity."
                )
                continue
            with _ltm_lock(f"{graph_id}/{node.name}"):
                # Re-opened inside the lock rather than written through the
                # copy this run has been holding since it started. `save()`
                # writes the whole corpus, and that copy was loaded minutes
                # ago — before the other items of a batch wrote theirs. Adding
                # to it and saving would put the store back as it was and lose
                # every entry written in between.
                if (task.get("memory") or {}).get("provider") == "mem0":
                    fresh = memory
                else:
                    fresh = memory_store.open_memory(graph_id, node.name, create=True)
                try:
                    _write_entry(fresh, graph_id, node.name, task, payload, as_message)
                    fresh.save()
                finally:
                    if fresh is not memory:
                        memory_store.close_memory(fresh)
        except Exception:
            state["memory_error"] = traceback.format_exc()


def _write_entry(memory, graph_id: str, node_name: str, task: dict,
                 payload: dict, as_message) -> None:
    """Store this run's selection under the subject it is about.

    A node that tracks a subject keeps one entry per subject — the subject's
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
            write_json(path, data)
            marked += 1
        except OSError:
            continue
    return marked


def _persist_run(state: dict) -> bool:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    data = _public(state)
    data.pop("persistence_error", None)
    try:
        write_json(RUNS_DIR / f"{state['run_id']}.json", data)
        state.pop("persistence_error", None)
        return True
    except OSError:
        return False


def retry_persistence(run_id):
    """Retry saving a settled in-memory Run without executing it again."""
    with _lock:
        state = _runs.get(run_id)
        return bool(state is not None and _persist_run(state))


def _public(state: dict) -> dict:
    """Run state for the API: its public keys. Node statuses are the walk's
    own — it marks each node running, completed or failed the moment that
    happens — never the framework graph's, which the walk does not drive and
    which would read "pending" until the whole run is over."""
    out = {k: v for k, v in state.items() if not k.startswith("_")}
    # Copies: the walk goes on changing its own while this is read elsewhere.
    out["nodes"] = [dict(n) for n in out.get("nodes") or []]
    return out


def set_evaluations(run_id: str, reports: dict) -> dict | None:
    """Keep evaluator reports computed later (on a saved run) with that run,
    beside the ones the run itself produced, and persist them."""
    with _lock:
        state = _runs.get(run_id)
        if state is not None:
            state['evaluations'] = {**(state.get('evaluations') or {}), **reports}
            _persist_run(state)
            return _public(state)
    stored = _read_run(run_id)
    if stored is None:
        return None
    stored['evaluations'] = {**(stored.get('evaluations') or {}), **reports}
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        write_json(RUNS_DIR / f"{run_id}.json", stored)
    except OSError:
        return stored
    return stored


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
            result = _public(state)
            if (state.get("_executing") and not state.get("_abandoned")
                    and result.get("status") in ("success", "failed", "cancelled")):
                # Completion includes Memory writes and the durable Run checkpoint.
                # A run the user has walked away from — dropped, or stopped
                # while inside something uninterruptible — is not waiting for
                # any of that: its status is what it was told, from now on.
                result["status"] = "running"
            return result
    return _read_run(run_id)


def list_runs(graph_id: str | None = None, limit: int | None = 20) -> list[dict]:
    """Recent runs (newest first, cap 20), merging in-memory and persisted runs.

    Which runs those are is decided on a small cached view of each file (its
    workflow and start time); only the runs returned are read in full.
    """
    return list_run_views(graph_id, limit, view=None)


def _run_meta(run: dict) -> dict:
    return {"graph_id": run.get("graph_id"), "created_at": run.get("created_at", "")}


def list_run_views(graph_id: str | None = None, limit: int | None = 20, view=None,
                   view_name: str | None = None) -> list:
    """Recent runs, newest first, each as `view(run)` — the whole run when
    `view` is None. A named view is cached per version of each run's file."""
    from backend.features.persistence import file_view
    found: dict[str, tuple] = {}
    if RUNS_DIR.is_dir():
        for path in RUNS_DIR.glob("*.json"):
            meta = file_view(path, "run-meta", _run_meta)
            if meta is not None:
                found[path.stem] = (meta, path)
    with _lock:
        live = {run_id: _public(state) for run_id, state in _runs.items()}
    for run_id, run in live.items():
        found[run_id] = (_run_meta(run), None)
    chosen = [(run_id, meta, path) for run_id, (meta, path) in found.items()
              if not graph_id or meta.get("graph_id") == graph_id]
    chosen.sort(key=lambda row: row[1].get("created_at") or "", reverse=True)
    if limit is not None:
        chosen = chosen[:limit]
    out = []
    for run_id, _meta, path in chosen:
        if path is None:
            run = live[run_id]
            out.append(view(run) if view else run)
        elif view is not None and view_name:
            value = file_view(path, view_name, view)
            if value is not None:
                out.append(value)
        else:
            run = _read_run(run_id)
            if run is not None:
                out.append(view(run) if view else run)
    return out
