"""Conversational workflow building for EvoAgentX Studio.

`POST /api/graphs/{id}/chat` takes the canvas graph the user is *currently
looking at* (which may hold unsaved edits), their message and the previous
turns, and returns the graph after the model's edits plus a plain-language
reply. Nothing is written to disk: the frontend applies the returned graph to
the canvas, and the user still saves explicitly.

The model does not use native function calling. `llm.chat()` returns a plain
string for every provider type in llm/providers.json and only some of those
providers support tool calls, so instead the model answers with a single JSON
object -- a reply plus a list of operations -- which this module parses,
validates and applies. That keeps the feature provider-agnostic and keeps
every mutation behind code that can reject it.

Operations: add_node / update_node / delete_node / rename_node / add_edge /
delete_edge / set_goal / set_name / auto_layout / create_tool /
generate_workflow (the framework's WorkFlowGenerator, for building a whole
workflow from a goal in one step).
"""

import copy
import json
import re
import sys
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException

import custom_tools
import model_json
import graphs as graph_store
import skills_api
import tools_registry

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# How many previous turns to replay. The graph itself is sent every turn, so
# older turns only carry intent, not state -- a short window is enough and
# keeps a long session from growing the prompt without bound.
HISTORY_TURNS = 12

# How many read-then-act rounds one message may take before the agent must
# answer. Enough for read -> diagnose -> patch -> verify; short enough that a
# model stuck in a reading loop cannot burn the user's budget.
MAX_STEPS = 6

router = APIRouter(prefix="/api")

# Whole-workflow generation is a planner call plus one agent call per subtask —
# minutes, not seconds. It runs on a worker thread and the client polls, the
# same shape batch.py and evolve_api.py use, so a chat turn never blocks on it.
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
JOB_TTL = 3600


class ChatError(Exception):
    """User-facing chat failure (HTTP 422)."""


# --------------------------------------------------------------------------
# prompt
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are the workflow architect inside EvoAgentX Studio. You build and adjust \
an agentic workflow on a visual canvas by emitting operations.

## The canvas

A workflow is a directed graph of tasks. Data flows by NAME: a task input is \
satisfied by an upstream task's output with the same name, or by a workflow \
input supplied at run time. Edges define execution order; the backend \
topologically sorts and runs them.

Three kinds of node:

1. LLM task (the default, no `kind` field) -- calls the model.
   {"name": "detect", "description": "...", "prompt": "...", \
"system_prompt": "...", "parse_mode": "json", \
"inputs": [{"name": "news", "type": "str", "description": "...", "required": true}], \
"outputs": [{"name": "finding", "type": "str", "description": "...", "required": true}], \
"tool_names": ["PythonInterpreterToolkit"], "skill_names": ["risk_rubric"]}
   - `prompt` references its inputs with SINGLE braces: {news}
   - When `parse_mode` is "json" the prompt must instruct the model to return \
JSON whose keys are exactly the output names.
   - `tool_names` are toolkit names from the catalog below; omit if unused.
   - `skill_names` are skills from the catalog below. A skill's instructions \
are appended to this node's system prompt at run time.
2. Source node -- `{"kind": "source", "source": {"type": ...}}`. Fetches \
input data at run start. Do not invent source types.
3. Tool node -- `{"kind": "tool", "tool": "<sub-tool name>"}`. Deterministic, \
no LLM call. Its inputs are the tool's parameters. A tool node may only take \
input from source nodes, other tool nodes, or workflow inputs -- never from \
an LLM task's output. Attach a tool to an LLM task via `tool_names` instead \
when it needs an LLM's output.

Names must be lowercase identifiers (letters, digits, underscore).

## Operations

Reply with ONE JSON object and nothing else:

{"reply": "<what you did or are asking, in the user's language>",
 "operations": [ ... ]}

Valid operations:

- {"op": "generate_workflow", "goal": "<goal sentence>"} -- builds an entire \
workflow from scratch with the framework's planner. Use this, alone, when the \
user asks for a workflow for a goal and the canvas is empty or they want to \
start over. It REPLACES every existing node.
  The `goal` must state what the workflow SHOULD ACCOMPLISH, as a task in the \
user's domain -- never "build/create a workflow that ...". The planner takes \
the goal literally, so a meta-phrased goal makes it plan the work of building \
a workflow ("analyse requirements", "generate implementation") instead of the \
work the user wants done. Rewrite the request accordingly:
    user: "build me a workflow that reviews a pull request diff"
    goal: "Review a pull request diff: summarise the change, flag risky edits, \
and draft review comments for the author"
- {"op": "add_node", "task": { ...task object as above..., "x": 0, "y": 0 }}
- {"op": "update_node", "name": "detect", "patch": {"prompt": "..."}} -- \
merges the patch into that task; only include fields you are changing.
- {"op": "delete_node", "name": "detect"}
- {"op": "rename_node", "name": "detect", "new_name": "classify"} -- edges follow.
- {"op": "add_edge", "source": "a", "target": "b"}
- {"op": "delete_edge", "source": "a", "target": "b"}
- {"op": "set_goal", "goal": "..."} / {"op": "set_name", "name": "..."}
- {"op": "auto_layout"} -- re-arranges every node by dependency depth. Add \
this after structural changes so the canvas stays readable.
- {"op": "create_tool", "spec": {"name": "text_stats", "code": "..."}} -- \
defines custom tools. A tool is a documented calling interface, so `code` is a \
Python MODULE and every public function in it becomes one tool: the module \
docstring says what the toolkit is, each function's name names a tool, its \
docstring is what a model reads to decide when to call it, its arguments are \
the parameters and its annotations (str/int/float/bool/dict/list) are their \
types. Document arguments under an `Args:` block. A leading underscore marks a \
helper. Nothing else is declared -- no separate description, no parameter \
list. Wrapping an installed library or an existing project is the point: \
import it and expose its API as documented functions. `name` is the toolkit's \
and may be omitted when the module has exactly one function. Code runs in a \
subprocess on the user's machine and must return JSON-serialisable values. \
Example code: \
"\\"\\"\\"Text statistics.\\"\\"\\"\\n\\n\\ndef word_count(text: str) -> dict:\\n    \
\\"\\"\\"Count the words in a text.\\n\\n    Args:\\n        text: the text to \
measure\\n    \\"\\"\\"\\n    return {\\"words\\": len(text.split())}\\n". After \
creating one, use a tool by name via a tool node, or attach the toolkit \
through a task's `tool_names`.
- {"op": "create_skill", "spec": {"name": "risk_rubric", "description": \
"When and how to grade credit-risk severity.", "content": "# Rubric\\n\\n..."}} \
-- defines a skill: standing Markdown instructions a node follows. Overwriting \
an existing skill keeps the previous version. Attach it with `skill_names`.
- {"op": "delete_skill", "name": "risk_rubric"}

Tools vs skills: a tool is code the workflow CALLS (deterministic, returns a \
value); a skill is instructions a node FOLLOWS (a taxonomy, a rubric, a house \
style). If the user describes judgement or standards, make a skill. If they \
describe a computation, an API call or hand you code, make a tool.

### Reading the workflow (these answer back to you)

Read operations return their result to you and you get another turn to act on \
it, up to a few steps. Use them to check your work and to debug.

- {"op": "validate"} -- run the same validation the Save button runs. Returns \
errors (empty when sound) and the workflow inputs the user would be asked for.
- {"op": "inspect_node", "name": "detect"} -- the node's full stored config.
- {"op": "list_runs"} -- recent runs of this workflow with status and time.
- {"op": "read_run"} or {"op": "read_run", "run_id": "..."} -- a run's outcome: \
status, error, and every node's status and output (truncated). Omit run_id for \
the most recent run. This is the main debugging tool: read what a node actually \
produced before guessing why the next one failed.
- {"op": "list_files", "path": "runs"} -- the workflow's workspace tree.
- {"op": "read_file", "path": "runs/<run>/output.json"} -- a workspace file \
(truncated).

### Acting outside the canvas

- {"op": "save_graph"} -- persist the workflow to disk. The canvas is otherwise \
only in the browser; save when the user asks, or after you finish a change they \
asked you to make permanent. Validation must pass first.
- {"op": "set_output_dir", "path": "runs"} -- where run artifacts are written \
inside the workspace.
- {"op": "write_file", "path": "files/seed.json", "content": "..."} -- create or \
overwrite a workspace text file. {"op": "delete_file", "path": ...} and \
{"op": "make_dir", "path": ...} also exist.
- {"op": "delete_tool", "name": "word_count"} -- remove a custom tool.
- {"op": "run_workflow", "inputs": {"topic": "..."}} -- propose running the \
workflow. This does NOT start it: the user gets a confirm button, because a run \
costs them money and its tools can touch files and external services. Supply \
every workflow input (`validate` lists them). Once they confirm, you are given \
the outcome in a following turn and can diagnose and fix. Say what you are about \
to run and why, so they can decide.

Rules:
- Emit only the operations needed for what was asked. An empty list is correct \
when you are only answering a question or need a clarification.
- Debugging loop: read_run to see what actually happened, form a hypothesis from \
the failing node's real inputs and output, patch with update_node, then validate \
(or run again if the user wants). Do not guess at a fix before reading the run.
- Never run the workflow unless the user asked for a run, a test or a debug. Runs \
cost the user money and their tools can touch files and external services.
- Prefer `update_node` over delete+add: it preserves position and unrelated fields.
- Every new node needs at least one output, or nothing downstream can consume it.
- Wire what you add: a node with no edges is inert and the canvas dims it.
- Keep `reply` short and concrete, in the user's language. Never put the JSON \
operations in `reply`.

## Available toolkits

%(tools)s

## Available skills

%(skills)s

## Source types

%(sources)s

## Current canvas

Goal: %(goal)s

%(graph)s
"""


def _tool_catalog() -> str:
    lines = []
    for entry in tools_registry.list_tools():
        if not entry.get("available"):
            continue
        subs = ", ".join(t["name"] for t in entry.get("tools", [])) or "-"
        lines.append(f"- {entry['name']}: {entry['description']} [sub-tools: {subs}]")
    return "\n".join(lines) or "(none available)"


def _skill_catalog() -> str:
    try:
        return "\n".join(
            f"- {s['name']}: {s['description']}" for s in skills_api.list_skills()
        ) or "(none yet -- create one with create_skill when standards are described)"
    except Exception as e:
        return f"(unavailable: {type(e).__name__})"


def _source_catalog() -> str:
    try:
        from source_apis import SOURCE_TYPE_SCHEMAS

        return "\n".join(
            f"- {type_}: {schema.get('description', '')} "
            f"[config: {', '.join(c['name'] for c in schema.get('config', []))}] "
            f"[outputs: {', '.join(schema.get('outputs', []))}]"
            for type_, schema in SOURCE_TYPE_SCHEMAS.items()
        ) or "(none)"
    except Exception as e:  # a broken source module must not break chat
        return f"(unavailable: {type(e).__name__})"


def _build_system_prompt(graph: dict) -> str:
    canvas = {"tasks": graph.get("tasks", []), "edges": graph.get("edges", [])}
    return SYSTEM_PROMPT % {
        "tools": _tool_catalog(),
        "skills": _skill_catalog(),
        "sources": _source_catalog(),
        "goal": graph.get("goal") or "(not set)",
        "graph": json.dumps(canvas, ensure_ascii=False, indent=2),
    }


# --------------------------------------------------------------------------
# operations
# --------------------------------------------------------------------------

def _slug(name: str) -> str:
    """Mirror of the frontend's sluggify(), so names round-trip unchanged."""
    s = re.sub(r"[^a-z0-9]+", "_", str(name or "").lower()).strip("_")
    return s or "node"


def _unique(base: str, taken: set) -> str:
    slug = _slug(base)
    if slug not in taken:
        return slug
    i = 2
    while f"{slug}_{i}" in taken:
        i += 1
    return f"{slug}_{i}"


def _find(tasks: list, name: str) -> dict | None:
    for t in tasks:
        if t.get("name") == name:
            return t
    return None


def _workflow_from_goal(goal: str, on_stage=None) -> dict:
    """Run the framework's WorkFlowGenerator and map the result onto the canvas.

    Driven through the generator's three public steps rather than the one-shot
    `generate_workflow()`, so the caller can report which stage is running --
    the agent stage is one LLM call per subtask and dominates the wait.

    One generated agent per node: the canvas has a single prompt per task, and
    the generated agent already carries the prompt, inputs and outputs.
    """
    from llm import get_evoagentx_llm
    from evoagentx.workflow.workflow_generator import WorkFlowGenerator

    def stage(text):
        if on_stage:
            on_stage(text)

    generator = WorkFlowGenerator(llm=get_evoagentx_llm())
    stage("Planning the tasks…")
    plan = generator.generate_plan(goal=goal)
    workflow = generator.build_workflow_from_plan(goal=goal, plan=plan)
    workflow._validate_workflow_structure()
    stage(f"Designing {len(workflow.nodes)} agents…")
    workflow = generator.generate_agents(goal=goal, workflow=workflow)
    workflow._validate_workflow_structure()
    stage("Laying out the canvas…")

    tasks, taken = [], set()
    renamed: dict[str, str] = {}
    for node in workflow.nodes:
        name = _unique(node.name, taken)
        taken.add(name)
        renamed[node.name] = name
        agent = (node.agents or [{}])[0]
        tasks.append({
            "name": name,
            "description": node.description or "",
            "prompt": agent.get("prompt", "") or "",
            "system_prompt": "",
            "parse_mode": "str",
            "inputs": [_param(p) for p in (agent.get("inputs") or node.inputs or [])],
            "outputs": [_param(p) for p in (agent.get("outputs") or node.outputs or [])],
            "tool_names": agent.get("tool_names") or [],
            "x": 0, "y": 0,
        })

    edges = []
    for edge in workflow.edges:
        src, tgt = renamed.get(edge.source), renamed.get(edge.target)
        if src and tgt and src != tgt:
            edges.append({"source": src, "target": tgt})
    return {"tasks": tasks, "edges": edges}


def _param(p) -> dict:
    """Framework Parameter (object or dict) -> canvas parameter dict."""
    d = p if isinstance(p, dict) else p.to_dict(ignore=["class_name"])
    return {
        "name": d.get("name", ""),
        "type": d.get("type") or "str",
        "description": d.get("description", "") or "",
        "required": bool(d.get("required", True)),
    }


MAX_CHARS = 2000


def _truncate(value, limit: int = MAX_CHARS):
    """Cap a value's size before it goes back into the model's context."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return value
    return text[:limit] + f"… (+{len(text) - limit} chars)"


def _run_digest(run: dict) -> dict:
    """A run reduced to what a debugging agent needs, small enough to reason over."""
    return {
        "run_id": run.get("run_id"),
        "status": run.get("status"),
        "error": _truncate(run.get("error")) if run.get("error") else None,
        "created_at": run.get("created_at"),
        "inputs": _truncate(run.get("inputs")),
        "nodes": [
            {"name": n.get("name"), "status": n.get("status"),
             "error": _truncate(n.get("error"), 600) if n.get("error") else None,
             "output": _truncate(n.get("output"), 900)}
            for n in (run.get("nodes") or [])
        ],
        "result": _truncate(run.get("result")),
    }


def apply_operations(graph: dict, operations: list, graph_id: str | None = None) -> dict:
    """Apply the model's operations to a copy of the graph.

    Returns a dict with the updated graph, per-operation notes, anything created,
    `observations` -- the results of read operations, which the caller feeds
    back so the model can act on what it found -- and `pending_run`, a run the
    model proposed for the user to confirm.

    A malformed single operation is reported as a note and skipped rather than
    failing the whole turn: one bad edge should not throw away four good nodes.
    """
    graph = copy.deepcopy(graph)
    notes: list[str] = []
    tools_created: list[str] = []
    skills_created: list[str] = []
    observations: list[dict] = []
    saved = False
    pending_run: dict | None = None

    def observe(op_name: str, payload):
        observations.append({"op": op_name, "result": payload})

    for raw in operations or []:
        if not isinstance(raw, dict):
            notes.append(f"Ignored a non-object operation: {raw!r}")
            continue
        op = raw.get("op")
        tasks = graph.setdefault("tasks", [])
        edges = graph.setdefault("edges", [])
        taken = {t.get("name") for t in tasks}

        try:
            if op == "generate_workflow":
                # Handled by the endpoint as a background job before this
                # function is reached; reaching it here means a stray op.
                notes.append("generate_workflow must be the only operation in a turn")

            elif op == "add_node":
                task = dict(raw.get("task") or {})
                if not task.get("name"):
                    raise ChatError("add_node needs a task with a name")
                name = _unique(task["name"], taken)
                if name != _slug(task["name"]):
                    notes.append(f"Renamed new node to '{name}' (name already taken)")
                task["name"] = name
                task.setdefault("x", 0)
                task.setdefault("y", 0)
                tasks.append(task)

            elif op == "update_node":
                task = _find(tasks, raw.get("name"))
                if task is None:
                    raise ChatError(f"update_node: no node named '{raw.get('name')}'")
                patch = dict(raw.get("patch") or {})
                patch.pop("name", None)  # renaming goes through rename_node
                task.update(patch)

            elif op == "delete_node":
                name = raw.get("name")
                if _find(tasks, name) is None:
                    raise ChatError(f"delete_node: no node named '{name}'")
                graph["tasks"] = [t for t in tasks if t.get("name") != name]
                graph["edges"] = [e for e in edges
                                  if e.get("source") != name and e.get("target") != name]

            elif op == "rename_node":
                task = _find(tasks, raw.get("name"))
                if task is None:
                    raise ChatError(f"rename_node: no node named '{raw.get('name')}'")
                new_name = _unique(raw.get("new_name") or "", taken - {task["name"]})
                old = task["name"]
                task["name"] = new_name
                for e in edges:
                    if e.get("source") == old:
                        e["source"] = new_name
                    if e.get("target") == old:
                        e["target"] = new_name

            elif op == "add_edge":
                src, tgt = raw.get("source"), raw.get("target")
                if _find(tasks, src) is None or _find(tasks, tgt) is None:
                    raise ChatError(f"add_edge: unknown node in {src} -> {tgt}")
                if src == tgt:
                    raise ChatError("add_edge: a node cannot feed itself")
                if not any(e.get("source") == src and e.get("target") == tgt for e in edges):
                    edges.append({"source": src, "target": tgt})

            elif op == "delete_edge":
                src, tgt = raw.get("source"), raw.get("target")
                graph["edges"] = [e for e in edges
                                  if not (e.get("source") == src and e.get("target") == tgt)]

            elif op == "set_goal":
                graph["goal"] = raw.get("goal") or ""

            elif op == "set_name":
                if raw.get("name"):
                    graph["name"] = raw["name"]

            elif op == "auto_layout":
                graph_store.auto_layout(graph)

            elif op == "create_skill":
                skills_api.save_skill(dict(raw.get("spec") or {}))
                skills_created.append((raw.get("spec") or {}).get("name"))

            elif op == "delete_skill":
                if not skills_api.delete_skill(raw.get("name") or ""):
                    raise ChatError(f"delete_skill: no skill named '{raw.get('name')}'")

            elif op == "create_tool":
                spec = custom_tools.validate_spec(
                    dict(raw.get("spec") or {}), tools_registry.builtin_names()
                )
                custom_tools.save_custom_tool(spec)
                tools_created.append(spec["name"])

            # ---- reads: results are fed back to the model ----
            elif op == "validate":
                errors = _validate(graph)
                inputs = []
                if not errors:
                    try:
                        _, inputs = graph_store.validate_graph(graph)
                    except Exception:
                        inputs = []
                observe("validate", {"errors": errors, "workflow_inputs": inputs})

            elif op == "inspect_node":
                task = _find(tasks, raw.get("name"))
                if task is None:
                    raise ChatError(f"inspect_node: no node named '{raw.get('name')}'")
                observe("inspect_node", task)

            elif op == "list_runs":
                import runner

                observe("list_runs", [
                    {"run_id": r.get("run_id"), "status": r.get("status"),
                     "created_at": r.get("created_at")}
                    for r in runner.list_runs(graph_id=graph_id)[:10]
                ])

            elif op == "read_run":
                import runner

                wanted = raw.get("run_id")
                if not wanted:
                    recent = runner.list_runs(graph_id=graph_id)
                    if not recent:
                        raise ChatError("read_run: this workflow has no runs yet")
                    wanted = recent[0].get("run_id")
                run = runner.get_run(wanted)
                if run is None:
                    raise ChatError(f"read_run: no run '{wanted}'")
                observe("read_run", _run_digest(run))

            elif op == "list_files":
                import workspace as workspace_mod

                if not graph_id:
                    raise ChatError("list_files: no workflow is open")
                tree = workspace_mod.tree(graph_id)
                prefix = raw.get("path")
                if prefix:
                    tree = [f for f in tree if str(f.get("path", "")).startswith(prefix)]
                observe("list_files", tree[:200])

            elif op == "read_file":
                import workspace as workspace_mod

                if not graph_id:
                    raise ChatError("read_file: no workflow is open")
                info = workspace_mod.read_file(graph_id, raw.get("path") or "")
                observe("read_file", {"path": raw.get("path"),
                                      "content": _truncate(info.get("content"))})

            # ---- acts outside the canvas ----
            elif op == "save_graph":
                if not graph_id:
                    raise ChatError("save_graph: no workflow is open")
                errors = _validate(graph)
                if errors:
                    raise ChatError("save_graph refused, the workflow is invalid: "
                                    + "; ".join(errors))
                graph_store.save_graph(graph_id, graph)
                saved = True

            elif op == "set_output_dir":
                graph["output_dir"] = (raw.get("path") or "runs").strip() or "runs"

            elif op == "write_file":
                import workspace as workspace_mod

                if not graph_id:
                    raise ChatError("write_file: no workflow is open")
                workspace_mod.write_file(graph_id, raw.get("path") or "",
                                         str(raw.get("content") or "").encode("utf-8"))
                notes.append(f"Wrote {raw.get('path')}")

            elif op == "delete_file":
                import workspace as workspace_mod

                if not graph_id:
                    raise ChatError("delete_file: no workflow is open")
                workspace_mod.delete_file(graph_id, raw.get("path") or "")
                notes.append(f"Deleted {raw.get('path')}")

            elif op == "make_dir":
                import workspace as workspace_mod

                if not graph_id:
                    raise ChatError("make_dir: no workflow is open")
                workspace_mod.make_dir(graph_id, raw.get("path") or "")

            elif op == "delete_tool":
                if not custom_tools.delete_custom_tool(raw.get("name") or ""):
                    raise ChatError(f"delete_tool: no custom tool '{raw.get('name')}'")

            elif op == "run_workflow":
                if not graph_id:
                    raise ChatError("run_workflow: no workflow is open")
                errors = _validate(graph)
                if errors:
                    raise ChatError("run_workflow refused, the workflow is invalid: "
                                    + "; ".join(errors))
                # Proposed, not started: a run spends the user's money and its
                # tools can touch files and external services, so the person
                # confirms it. The client starts it and reports the outcome
                # back in a following turn.
                pending_run = {"inputs": dict(raw.get("inputs") or {})}

            else:
                notes.append(f"Ignored unknown operation {op!r}")
        except custom_tools.CustomToolError as e:
            notes.append(f"create_tool failed: {e}")
        except skills_api.SkillError as e:
            notes.append(f"{op} failed: {e}")
        except ChatError as e:
            notes.append(str(e))
        except Exception as e:  # one bad operation must not lose the others
            notes.append(f"{op} failed: {type(e).__name__}: {e}")

    return {
        "graph": graph,
        "notes": notes,
        "tools_created": tools_created,
        "skills_created": skills_created,
        "observations": observations,
        "saved": saved,
        "pending_run": pending_run,
    }


# --------------------------------------------------------------------------
# endpoint
# --------------------------------------------------------------------------

def _ask(messages: list) -> str:
    from llm import chat as llm_chat
    from llm.registry import ProviderError

    try:
        return llm_chat(None, messages)
    except ProviderError as e:
        raise ChatError(f"LLM provider is not configured: {e}")
    except Exception as e:
        raise ChatError(f"LLM call failed: {type(e).__name__}: {e}")


def _validate(graph: dict) -> list[str]:
    """Validation errors for a candidate graph, or [] when it is sound."""
    try:
        graph_store.validate_graph(graph)
        return []
    except graph_store.GraphValidationError as e:
        return [str(x) for x in e.errors]
    except Exception as e:
        return [f"{type(e).__name__}: {e}"]


@router.post("/graphs/{graph_id}/chat")
def chat(graph_id: str, body: dict = Body(...)):
    if not graph_store.graph_exists(graph_id):
        raise HTTPException(status_code=404, detail=f"Graph '{graph_id}' not found")

    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=422, detail="message is required")

    # The client sends the canvas it is looking at, which may hold unsaved
    # edits; falling back to the stored graph only if it sent nothing.
    graph = body.get("graph") or graph_store.load_graph(graph_id) or {}
    history = [m for m in (body.get("history") or [])
               if isinstance(m, dict) and m.get("role") in ("user", "assistant")]

    messages = [{"role": "system", "content": _build_system_prompt(graph)}]
    messages += [{"role": m["role"], "content": str(m.get("content") or "")}
                 for m in history[-HISTORY_TURNS:]]
    messages.append({"role": "user", "content": message})

    try:
        answer = model_json.extract_json(_ask(messages))
    except (ChatError, ValueError) as e:
        raise HTTPException(status_code=422, detail=str(e))

    reply = str(answer.get("reply") or "").strip()
    operations = answer.get("operations") or []
    if not isinstance(operations, list):
        operations, reply = [], reply or "The model returned malformed operations."

    # Whole-workflow generation takes minutes; hand it to a job and let the
    # client poll instead of holding the request open.
    generate = next((o for o in operations
                     if isinstance(o, dict) and o.get("op") == "generate_workflow"), None)
    if generate is not None:
        existing_job = running_job_for(graph_id)
        goal = (generate.get("goal") or graph.get("goal") or "").strip()
        if len(goal) < 10:
            return {
                "reply": reply, "operations": [], "graph": graph, "applied": False,
                "errors": ["To generate a workflow I need a goal of at least 10 characters."],
                "notes": [], "tools_created": [], "skills_created": [],
                "saved": False, "pending_run": None, "steps": 0,
            }
        job_id = start_generation(graph, goal, graph_id)
        already = bool(existing_job and existing_job["job_id"] == job_id)
        return {
            "reply": reply or "Generating the workflow…",
            "operations": [generate],
            "graph": graph,
            "applied": False,
            "errors": [],
            "notes": ([] if len(operations) == 1 else
                      ["Other operations in this turn were skipped: generating a "
                       "workflow replaces the whole canvas."])
                     + (["A generation was already running for this workflow — "
                         "watching that one instead of starting another."]
                        if already else []),
            "tools_created": [],
            "skills_created": [],
            "saved": False,
            "pending_run": None,
            "steps": 0,
            "job_id": job_id,
        }

    # Agent loop: read operations answer back, and the model gets another turn to
    # act on what it found. Bounded so a model that keeps reading cannot spin.
    updated, notes = graph, []
    tools_created: list[str] = []
    skills_created: list[str] = []
    steps: list[dict] = []
    saved = False
    pending_run = None

    for step in range(MAX_STEPS):
        result = apply_operations(updated, operations, graph_id)
        updated = result["graph"]
        notes += result["notes"]
        tools_created += result["tools_created"]
        skills_created += result["skills_created"]
        saved = saved or result["saved"]
        pending_run = result["pending_run"] or pending_run
        if operations:
            steps.append({"operations": operations, "reply": reply})

        observations = result["observations"]
        # Stop when the model has nothing left to look at, ran out of steps, or
        # proposed a run (which needs the user before anything else happens).
        if not observations or pending_run or step == MAX_STEPS - 1:
            break

        messages.append({"role": "assistant", "content": json.dumps(answer, ensure_ascii=False)})
        messages.append({"role": "user", "content":
            "Results of your read operations:\n"
            + json.dumps(observations, ensure_ascii=False, indent=2)
            + "\n\nContinue: act on what you found, or reply with an empty "
              "operations list if you are done."})
        try:
            answer = model_json.extract_json(_ask(messages))
        except ChatError as e:
            notes.append(str(e))
            break
        reply = str(answer.get("reply") or reply).strip()
        operations = answer.get("operations") or []
        if not isinstance(operations, list):
            operations = []

    all_operations = [op for s in steps for op in s["operations"]]
    errors = _validate(updated) if all_operations else []

    # One repair round: the model sees exactly what the validator rejected and
    # rewrites its operations. A second round rarely helps and doubles the wait,
    # so a still-broken result is returned unapplied with the errors shown.
    if errors:
        repair = messages + [
            {"role": "assistant", "content": json.dumps(answer, ensure_ascii=False)},
            {"role": "user", "content":
                "Those operations produced an invalid workflow:\n"
                + "\n".join(f"- {e}" for e in errors)
                + "\n\nReturn a corrected JSON object with the same reply and fixed "
                  "operations, applied to the ORIGINAL canvas shown above."},
        ]
        try:
            retry_answer = model_json.extract_json(_ask(repair))
            retry_ops = retry_answer.get("operations") or []
            if isinstance(retry_ops, list) and retry_ops:
                retry = apply_operations(graph, retry_ops, graph_id)
                retry_errors = _validate(retry["graph"])
                if not retry_errors:
                    updated, errors = retry["graph"], []
                    notes = notes + retry["notes"]
                    all_operations = retry_ops
                    tools_created += retry["tools_created"]
                    skills_created += retry["skills_created"]
                    saved = saved or retry["saved"]
                    reply = str(retry_answer.get("reply") or reply).strip()
        except ChatError:
            pass  # keep the first attempt's errors for the user to see

    return {
        "reply": reply or ("Done." if all_operations else ""),
        "operations": all_operations,
        # `graph` is the canvas to show; on failure it is the unchanged input,
        # so a rejected turn never half-applies.
        "graph": graph if errors else updated,
        "applied": not errors and json.dumps(updated, sort_keys=True) != json.dumps(graph, sort_keys=True),
        "errors": errors,
        "notes": notes,
        "tools_created": tools_created,
        "skills_created": skills_created,
        "saved": saved and not errors,
        "pending_run": pending_run,
        "steps": len(steps),
    }


# --------------------------------------------------------------------------
# generation jobs
# --------------------------------------------------------------------------

def _reap_jobs() -> None:
    cutoff = time.time() - JOB_TTL
    with _jobs_lock:
        for job_id in [k for k, v in _jobs.items() if v["updated_at"] < cutoff]:
            _jobs.pop(job_id, None)


def _set_job(job_id: str, **fields) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is not None:
            job.update(fields, updated_at=time.time())


def running_job_for(graph_id: str) -> dict | None:
    """The generation already in flight for this workflow, if any."""
    _reap_jobs()
    with _jobs_lock:
        for job in _jobs.values():
            if job.get("graph_id") == graph_id and job["status"] == "running":
                return dict(job)
    return None


def start_generation(graph: dict, goal: str, graph_id: str | None = None) -> str:
    """Kick off whole-workflow generation; returns the job id to poll.

    One at a time per workflow: generation takes minutes, and a page reload
    loses the client's poller, so the natural reaction is to ask again. Without
    this guard each ask started another job — several plans racing, all billed,
    all slower for competing over the same model.
    """
    existing = running_job_for(graph_id) if graph_id else None
    if existing:
        return existing["job_id"]

    _reap_jobs()
    job_id = uuid.uuid4().hex[:12]
    started = time.time()
    with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id, "graph_id": graph_id, "status": "running",
            "stage": "Starting…", "graph": None, "error": None,
            "started_at": started, "updated_at": started,
        }

    def work():
        try:
            generated = _workflow_from_goal(
                goal, on_stage=lambda text: _set_job(job_id, stage=text)
            )
            result = copy.deepcopy(graph)
            result["goal"] = goal
            result["tasks"] = generated["tasks"]
            result["edges"] = generated["edges"]
            graph_store.auto_layout(result)
            errors = _validate(result)
            if errors:
                _set_job(job_id, status="failed",
                         error="The generated workflow did not validate: "
                               + "; ".join(errors))
            else:
                _set_job(job_id, status="done", stage="Done", graph=result)
        except Exception as e:
            _set_job(job_id, status="failed", error=f"{type(e).__name__}: {e}")

    threading.Thread(target=work, daemon=True).start()
    return job_id


@router.get("/graphs/{graph_id}/chat/jobs")
def running_generation(graph_id: str):
    """The generation in flight for this workflow, so a reloaded page can
    re-attach instead of starting a second one."""
    job = running_job_for(graph_id)
    if job is None:
        return {"job_id": None}
    job["elapsed"] = int(time.time() - job["started_at"])
    job.pop("graph", None)
    return job


@router.get("/graphs/{graph_id}/chat/jobs/{job_id}")
def generation_job(graph_id: str, job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown or expired job")
        job = dict(job)
    job["elapsed"] = int(time.time() - job["started_at"])
    return job
