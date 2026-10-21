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
delete_edge / set_goal / set_name / auto_layout / create_tool / Chat Agent
create, update, connect-memory, inspect and delete (agent_ops.py) /
generate_workflow (the framework's WorkFlowGenerator, for building a whole
workflow from a goal in one step).
"""

import copy
import json
import re
import threading
import time
import uuid

from fastapi import APIRouter, Body, HTTPException

from backend.features.workflow.graphs import history_ids
from backend.api import custom_tools
from backend.api import model_json, chat_control
from backend.api.chat_engine import ChatEngine
from backend.features.chat import agent_ops, node_resolver, turn_jobs
from backend.api import graphs as graph_store
from backend.api import skills_api
from backend.api import tools_registry

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
_job_controls = {}
JOB_TTL = 3600


class ChatError(Exception):
    """User-facing chat failure (HTTP 422).

    `candidates` are the canvas objects a failed name lookup could have
    meant; they go back to the model with the error so it can retry or ask.
    """

    def __init__(self, message, candidates=None):
        super().__init__(message)
        self.candidates = candidates or []


# --------------------------------------------------------------------------
# prompt
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You share the same computation tools as the Results assistant. For calculations,
use {"op":"compute","scope":"all","code":"print(len(records))"}.
records contains all selected saved run dictionaries with decoded result, steps,
source_inputs and expected_label. scope can be all/current/batch; current/batch require run_id.
Python standard-library code can compute custom accuracy, precision, recall, F1,
grouping and formulas. Print evidence, denominator and exclusions. Inspect actual
predictions and ground truth; never infer accuracy from workflow success status.
Execution is sandboxed, without network or project files, and limited to 20 seconds.
You are the workflow architect inside EvoAgentX Studio. You build and adjust \
an agentic workflow on a visual canvas by emitting operations.

## The canvas

A workflow is a directed graph of tasks. Data crosses explicit edge mappings
(from an output field to an input field). Control-only edges order tasks without
passing data. Inputs not fed by an edge are workflow inputs supplied at run time.
Do not infer connections just because field names match.

DataLoader source: {"kind":"source","source":{"type":"dataloader","resource_id":"uploaded resource ID","loader":"python","code":"<build_dataset(resource, config) returning a PyTorch Dataset>","n":0},"outputs":[...]}. Uploaded resources are chosen by the user. Never invent resource IDs. Loader owns transforms, selection and order; Run owns execution settings. Create new DataLoader inputs only with loader="python". Legacy reader modes are for existing saved inputs only. All loader modes use PyTorch DataLoader with drop_last=False and dictionary-preserving collation. For pasted Python use loader="python", code defining build_dataset(resource, config) returning torch.utils.data.Dataset or IterableDataset, and read_batch_size. Prefer named typed build_dataset arguments (resource, path: str, split: str = "dev") so Studio can generate the input form; resource is automatic. Legacy config keys are also supported. Each item must be a JSON-compatible dictionary; do not return an already batched DataLoader. Preview determines output fields and types, never guess them. Code is saved on the Input, not in the model adapter.
New reusable Python tools may define build_tool(typed configuration parameters), returning an object with synchronous run(inputs). Declare literal INPUT_SCHEMA and OUTPUT_SCHEMA lists of {name,type,required,nullable}; types use str/int/float/bool/dict/list. Only the factory is exported under the saved tool name; other functions/classes are internal. Config is shared by canvas, Agent and Chat callers. Existing function toolkits remain supported.
Evaluation is not on the canvas. A workflow's evaluators live in the Evaluate & Evolve panel (the graph document's "evaluators" list, each {name, code, config, metric, direction, timing, timeout, labels, enabled}), never as a node. Never create a node of kind "evaluator" and never connect anything to one. If the user asks for evaluation, say that it is written as Python code in Evaluate & Evolve: build_evaluator(**typed parameters) returning an object with evaluate(records), or evaluate(records, **typed parameters), returning {"metrics": {name: number}} with optional records, details and coverage. The code receives complete saved executions (inputs, result, all nodes and node_outputs, status/errors, execution_snapshot) and owns its own grouping and aggregation. Evolve selects one of those evaluators as its objective.

Three kinds of node:

1. LLM task (the default, no `kind` field) -- calls the model.
   {"name": "detect", "description": "...", "prompt": "...", \
"system_prompt": "...", "parse_mode": "json", \
"inputs": [{"name": "news", "type": "str", "description": "...", "required": true}], \
"outputs": [{"name": "finding", "type": "str", "description": "...", "required": true}], \
"tool_names": ["PythonInterpreterToolkit"], "skill_names": ["style_guide"]}
   - `prompt` references its inputs with SINGLE braces: {news}
   - When `parse_mode` is "json" the prompt must instruct the model to return \
JSON whose keys are exactly the output names.
   - `tool_names` are toolkit names from the catalog below; omit if unused.
   - `skill_names` are skills from the catalog below. A skill's instructions \
are appended to this node's system prompt at run time.
2. Source node -- `{"kind": "source", "source": {"type": ...}}`. Fetches \
input data at run start. Do not invent source types.
3. Tool node -- `{"kind": "tool", "tool": "<sub-tool name>"}`. Deterministic, \
no LLM call. Its inputs are the tool's parameters. It can consume explicitly mapped outputs from sources, tools or LLM tasks.

Names must be lowercase identifiers (letters, digits, underscore).

## Continuing this conversation
The current canvas below is authoritative, including unsaved edits. Use conversation history
and recorded operation outcomes to interpret follow-up requests. On a nonempty canvas,
modify the existing nodes with incremental operations; do not generate a replacement unless
the user explicitly asks to start over. If a previous edit failed, it was not applied.

## Operations

Reply with ONE JSON object and nothing else:

{"reply": "<what you did or are asking, in the user's language>",
 "operations": [ ... ]}

Valid operations:

- {"op": "generate_workflow", "goal": "<goal sentence>"} -- builds an entire \
workflow from scratch with the framework's planner. Use this, alone, when the \
user asks for a workflow for a goal and the canvas is empty or they want to \
start over. It REPLACES every existing node. It runs in the background for a \
few minutes: your reply must say the workflow is being generated and will \
appear on the canvas when it is done -- never that it has been created.
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
- {"op": "add_edge", "source": "a", "target": "b", "mappings": [{"from": "output_field", "to": "input_field"}]} -- create or update this edge.
  Field names must exist. For ordering only use "control_only": true instead.
  Without either, same-name fields are mapped automatically; an edge with no shared fields is control-only.
- {"op": "delete_edge", "source": "a", "target": "b"}
Names you are given may differ slightly from the stored ones (case, spaces, \
another language); operations resolve them and report what they matched. When \
an operation reports that nothing matched or several things did, it lists the \
candidates: use the exact id, or ask the user which one they meant. Never \
claim a change was made unless an operation for it was applied.
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
- {"op": "create_skill", "spec": {"name": "style_guide", "description": \
"How findings are graded and worded.", "content": "# Guide\\n\\n..."}} \
-- defines a skill: standing Markdown instructions a node follows. Overwriting \
an existing skill keeps the previous version. Attach it with `skill_names`.
- {"op": "delete_skill", "name": "style_guide"}

Tools vs skills: a tool is code the workflow CALLS (deterministic, returns a \
value); a skill is instructions a node FOLLOWS (a taxonomy, a rubric, a house \
style). If the user describes judgement or standards, make a skill. If they \
describe a computation, an API call or hand you code, make a tool.

When creating a tool, include spec.requirements (explicit pip package names/version constraints,
never infer a package name from an import) and spec.tests: [{"tool":"word_count",
"args":{"text":"hello world"},"expected":{"words":2}}]. Provide representative small,
non-destructive examples for every exported function, including an edge case. Missing
credentials or required real resources must be reported, never invented. Declared dependencies
are installed automatically in a per-tool directory, without changing Studio's environment.
Creation returns an actual verification report to you. Fix failing code and recreate it;
only call it verified if verification.status is verified. No tests means unverified;
a successful smoke call alone is not correctness verification. Verification covers examples only.
- {"op":"verify_tool","name":"text_stats"} -- rerun saved examples and dependency installation.

### Chat Agents

Chat Agents (listed under canvas_agents, with their provider, tools, skills and connected memories) are independent conversations placed on the canvas, not workflow nodes: no edges, not part of runs. Address one by its name or its chat:<id>. A Chat Agent's memory connections are its "memories" list; connect it only to a memory this canvas has (list_memories shows them: shared memories, which it can read and write, and workflow memories, which it can only read).

- {"op": "create_agent", "name": "Researcher", "instructions": "...", "provider": null, "max_steps": 20, "timeout": 300, "tools": ["web_search"], "skill_names": ["style_guide"], "memories": [{"memory": "<memory name or id>", "read": true, "write": false}], "x": 400, "y": 250} -- add a Chat Agent to the canvas. Only name is needed; omit the rest for the defaults (platform provider, max_steps 1-100 default 20, timeout 10-1800 s default 300, every available tool, no skills, no memory, placed beside the other Agents). "tools" lists tool or toolkit names from the catalog (null = all available tools, [] = none); "skill_names" lists skills from the catalog; a memory may also be given as just its name. Later operations in this turn can address the new Agent by its name.
- {"op": "update_agent", "name": "<Chat Agent name or chat:id>", "patch": {"name": "...", "instructions": "...", "provider": "...", "max_steps": 20, "timeout": 300, "tools": [...], "skill_names": [...], "memories": [...], "x": 0, "y": 0}} -- change only the supplied settings. A list in the patch REPLACES the whole list; to add or remove one item use the operations below.
- {"op": "connect_agent_memory", "agent": "<Chat Agent>", "memory": "<memory name or id>", "read": true, "write": false} -- connect a Chat Agent to a memory (keeps its other memories). write: true only for a shared memory.
- {"op": "disconnect_agent_memory", "agent": "<Chat Agent>", "memory": "<memory name or id>"} -- remove that connection; add "direction": "read" or "write" to drop only one direction.
- {"op": "add_agent_tool", "agent": "<Chat Agent>", "tool": "<tool or toolkit name>"} / {"op": "remove_agent_tool", ...} -- one tool (a toolkit name means all of its tools). An Agent with tools "all available tools" already has every tool.
- {"op": "add_agent_skill", "agent": "<Chat Agent>", "skill": "<skill>"} / {"op": "remove_agent_skill", ...}
- {"op": "delete_agent", "name": "<Chat Agent>"} -- remove a Chat Agent from the canvas (its conversations are archived, not erased).
- {"op": "inspect_agent", "name": "<Chat Agent>"} -- answers back with the Agent's full settings, its connected memories by name and whether each is read and/or written. Use it to answer questions such as "what memory does X use".
- {"op": "list_agents"} -- answers back with every Chat Agent, compactly.

delete_node, rename_node and inspect_node also accept Chat Agents. An operation that fails (an unknown memory, tool or skill, a value out of range) answers back with the reason and the valid choices: retry with one of them or ask the user.

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
- {"op": "plan_workflow", "inputs": {}, "start_at": []} -- inspect the actual execution plan, required inputs, nodes, warnings and missing/invalid values; no execution.
- {"op": "run_workflow", "inputs": {}, "start_at": [], "record": 0, "session": "optional"} -- prepare an editable run card with a validated plan. Omit record unless choosing a source record. The user can adjust values and click Run it. Never claim a run started before a run_id is returned. Only request this when the user asks to run or retry.
- {"op": "cancel_run", "run_id": "..."} -- stop a run belonging to this workflow. Omit run_id to stop the most recent active run.
- {"op": "list_batches"}, {"op": "read_batch", "batch_id": "..."}, {"op": "cancel_batch", "batch_id": "..."} -- inspect and stop this workflow's batches.
- {"op": "configure_workflow", "patch": {"goal": "...", "preprocess": null, "output_dir": "runs"}} -- change only supplied workflow settings.
- {"op": "list_memories"}, {"op": "read_memory", "memory_id": "mem:investigate", "query": "", "offset": 0, "limit": 20} -- inspect this workflow's Memory; table history can be paginated.
- {"op": "read_schedule"} -- inspect scheduling for this workflow.
- {"op": "set_schedule", "schedule": {"mode": "interval", "interval_minutes": 60, "enabled": true, "inputs": {}}} -- save the current workflow and schedule it ONLY when explicitly requested. Daily mode uses time "HH:MM" in the server timezone. Minimum interval is 5 minutes. Validate inputs first.
- {"op": "clear_schedule"} -- remove this workflow's schedule when requested.
- {"op": "read_watch"}, {"op": "start_watch"}, {"op": "stop_watch"} -- inspect or control configured source watchers. Start only if the user asks for ongoing monitoring.
- {"op": "open_panel", "panel": "run"} -- open workflow controls. Allowed panels: run, runs, schedule, evolve, review, workspace. Use run for batch input uploads and batch settings, schedule for timed runs, workspace for input/output files.

Configuration changes use update_node: prompt/system_prompt, inputs/outputs, enabled,
tool_names, skill_names, memory/use_long_term_memory, source settings or harness.
Inspect the node first. Keep unrelated configuration intact; do not invent unsupported fields.
For run parameters use plan_workflow; ask for missing values instead of inventing data.
For retries read the failed run, fix its cause, then prepare run_workflow using its inputs.
Do not start repeated runs automatically. Stop/cancel only when the user requests it.

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
- Wire intended data dependencies explicitly. A node with no edges can still execute unless enabled is false.
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
        from backend.api.source_apis import SOURCE_TYPE_SCHEMAS

        return "\n".join(
            f"- {type_}: {schema.get('description', '')} "
            f"[config: {', '.join(c['name'] for c in schema.get('config', []))}] "
            f"[outputs: {', '.join(schema.get('outputs', []))}]"
            for type_, schema in SOURCE_TYPE_SCHEMAS.items()
        ) or "(none)"
    except Exception as e:  # a broken source module must not break chat
        return f"(unavailable: {type(e).__name__})"


def _agent_listing(agents: list, graph: dict | None = None) -> list:
    """Each canvas Chat Agent with the settings the assistant reasons about:
    provider, tools, skills and connected memories (by name)."""
    agents = [a for a in agents or [] if isinstance(a, dict) and a.get("id")]
    resources = agent_ops.memory_catalog(graph) if graph is not None and any(
        a.get("memories") for a in agents) else []
    return [agent_ops.summary(a, resources) for a in agents]


def _canvas_summary(graph: dict, agents: list) -> str:
    """The canvas as it stands mid-turn, compact enough to repeat each step."""
    def fields(params):
        return [p.get("name") for p in params or [] if isinstance(p, dict)]
    tasks = [{"name": t.get("name"), **({"kind": t["kind"]} if t.get("kind") else {}),
              "description": str(t.get("description") or "")[:120],
              "inputs": fields(t.get("inputs")), "outputs": fields(t.get("outputs")),
              **({"enabled": False} if t.get("enabled") is False else {})}
             for t in graph.get("tasks", []) if isinstance(t, dict)]
    edges = [{k: e[k] for k in ("source", "target", "mappings", "control_only") if k in e}
             for e in graph.get("edges", []) if isinstance(e, dict)]
    return json.dumps({"goal": graph.get("goal"), "tasks": tasks, "edges": edges,
                       "canvas_agents": _agent_listing(agents, graph)}, ensure_ascii=False, default=str)


def _build_system_prompt(graph: dict, agents: list | None = None) -> str:
    canvas = {"tasks": graph.get("tasks", []), "edges": graph.get("edges", []),
              "output_dir": graph.get("output_dir", "runs"), "preprocess": graph.get("preprocess"),
              "memory_resources": graph.get("memory_resources", []),
              "canvas_agents": _agent_listing(agents or [], graph)}
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
    from backend.features.persistence import name_tag
    s = re.sub(r"[^a-z0-9]+", "_", str(name or "").lower()).strip("_")
    tag = name_tag(name)
    if tag:
        return f"{s}_{tag}" if s else f"node_{tag}"
    return s or "node"


def _save_graph(graph_id: str, graph: dict) -> dict:
    """Save as PUT /api/graphs/{id} does: the workspace is compiled from the
    saved canvas too, or it keeps the previous version until a manual save."""
    saved = graph_store.save_graph(graph_id, graph)
    try:
        from backend.api import workspace
        workspace.write_project(saved)
    except Exception:  # noqa: BLE001 - a half-built workflow must still save
        pass
    return saved


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


def _resolve(op: str, query, tasks: list, agents: list = (), *, destructive: bool = False,
             notes: list | None = None) -> dict:
    """The canvas object an operation names (node_resolver), or ChatError.

    Returns the resolver's candidate: `kind` is the task kind or
    "chat_agent", `ref` the task/agent dict itself.
    """
    candidates = node_resolver.task_candidates(tasks) + node_resolver.agent_candidates(agents)
    result = node_resolver.resolve(query, candidates, destructive=destructive)
    if result["status"] != "ok":
        raise ChatError(node_resolver.failure(op, query, result), result["candidates"])
    if result.get("note") and notes is not None:
        notes.append(f"{op}: {result['note']}")
    return result["match"]


def _resolve_task(op: str, query, tasks: list, agents: list = (), *, destructive: bool = False,
                  notes: list | None = None) -> dict:
    """Like _resolve, for operations that only apply to workflow nodes."""
    match = _resolve(op, query, tasks, agents, destructive=destructive, notes=notes)
    if match["kind"] == "chat_agent":
        raise ChatError(f"{op}: '{query}' is the Chat Agent {match['id']}, not a workflow node. "
                        "Use update_agent / delete_agent for Chat Agents.",
                        [node_resolver.summary(match)])
    return match["ref"]


def _workflow_from_goal(goal: str, on_stage=None) -> dict:
    """Run the framework's WorkFlowGenerator and map the result onto the canvas.

    Driven through the generator's three public steps rather than the one-shot
    `generate_workflow()`, so the caller can report which stage is running --
    the agent stage is one LLM call per subtask and dominates the wait.

    One generated agent per node: the canvas has a single prompt per task, and
    the generated agent already carries the prompt, inputs and outputs.
    """
    from backend.features.model_bridge import workflow_model
    from evoagentx.workflow.workflow_generator import WorkFlowGenerator

    def stage(text):
        if on_stage:
            on_stage(text)

    generator = WorkFlowGenerator(llm=workflow_model())
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


# Operations that change the canvas graph itself. These are what a turn's
# result replays onto a canvas the person edited while the turn ran; reads and
# side effects (files, tools, runs, saves) already happened and are not redone.
CANVAS_OPS = ("add_node", "update_node", "delete_node", "rename_node", "add_edge",
              "delete_edge", "set_goal", "set_name", "auto_layout",
              "configure_workflow", "set_output_dir")


def apply_operations(graph: dict, operations: list, graph_id: str | None = None,
                     agents: list | None = None) -> dict:
    """Apply the model's operations to a copy of the graph.

    Returns a dict with the updated graph, per-operation notes, anything created,
    `observations` -- the results of read operations, which the caller feeds
    back so the model can act on what it found -- and `pending_run`, a run the
    model proposed for the user to confirm.

    Chat Agents on the canvas are not part of the workflow graph: they live in
    the harness store and the frontend owns their canvas state. `agents` (or
    the graph's `canvas_agents`) is the list the assistant can see; an
    operation on one comes back in `agent_actions` for the frontend to carry
    out through the same calls its own buttons use, and `canvas_agents` is the
    list as it stands after this batch.

    `canvas_operations` are the canvas edits that took effect, with names as
    resolved, so the client can replay them onto a canvas that changed meanwhile.

    A malformed single operation is reported as a note and skipped rather than
    failing the whole turn: one bad edge should not throw away four good nodes.
    """
    graph = copy.deepcopy(graph)
    listed = graph.pop("canvas_agents", None)
    agents = copy.deepcopy(listed if agents is None else agents) or []
    notes: list[str] = []
    tools_created: list[str] = []
    skills_created: list[str] = []
    observations: list[dict] = []
    agent_actions: list[dict] = []
    canvas_operations: list[dict] = []
    saved = False
    pending_run: dict | None = None
    panel = None

    def observe(op_name: str, payload):
        observations.append({"op": op_name, "result": payload})

    def remove_agent(match):
        agents[:] = [a for a in agents if node_resolver.agent_candidates([a])[0]["id"] != match["id"]]
        name = (match["ref"] or {}).get("name")
        agent_actions.append({"action": "remove", "id": match["id"][len("chat:"):], "name": name,
                              "summary": f"removed Chat Agent '{name or match['id']}'"})
        notes.append(f"Removed Chat Agent '{(match['ref'] or {}).get('name') or match['id']}' "
                     "(its conversations are archived, not erased)")

    agent_ops_ = agent_ops.AgentOps(graph, agents, agent_actions, notes, observe, _resolve)

    for raw in operations or []:
        chat_control.check()
        if not isinstance(raw, dict):
            notes.append(f"Ignored a non-object operation: {raw!r}")
            continue
        op = raw.get("op")
        tasks = graph.setdefault("tasks", [])
        edges = graph.setdefault("edges", [])
        taken = {t.get("name") for t in tasks}
        applied = None  # the canvas edit, as resolved, when it took effect

        try:
            if op == "generate_workflow":
                # Handled by the endpoint as a background job before this
                # function is reached; reaching it here means a stray op.
                notes.append("generate_workflow must be the only operation in a turn")

            elif op in ("compute", "aggregate"):
                from backend.api import result_chat
                scoped = result_chat.scoped_records(graph_id, raw.get("scope", "all"), raw.get("run_id"))
                observe(op, result_chat.operate(scoped, raw))

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
                applied = raw

            elif op == "update_node":
                task = _resolve_task("update_node", raw.get("name"), tasks, agents, notes=notes)
                patch = dict(raw.get("patch") or {})
                patch.pop("name", None)  # renaming goes through rename_node
                task.update(patch)
                applied = {**raw, "name": task["name"]}

            elif op in ("delete_node", "delete_agent"):
                match = _resolve(op, raw.get("agent") or raw.get("name") or raw.get("id"),
                                 tasks if op == "delete_node" else [],
                                 agents, destructive=True, notes=notes)
                if match["kind"] == "chat_agent":
                    remove_agent(match)
                else:
                    name = match["id"]
                    graph["tasks"] = [t for t in tasks if t.get("name") != name]
                    graph["edges"] = [e for e in edges
                                      if e.get("source") != name and e.get("target") != name]
                    applied = {"op": "delete_node", "name": name}

            elif agent_ops_.apply(op, raw):
                pass

            elif op == "rename_node":
                match = _resolve("rename_node", raw.get("name"), tasks, agents, notes=notes)
                if match["kind"] == "chat_agent":
                    agent_ops_.update("rename_node", raw, {"name": raw.get("new_name")}, match=match)
                else:
                    task = match["ref"]
                    new_name = _unique(raw.get("new_name") or "", taken - {task["name"]})
                    old = task["name"]
                    task["name"] = new_name
                    from backend.features.memory.identity import rename_references
                    rename_references(graph, old, new_name)
                    for e in edges:
                        if e.get("source") == old:
                            e["source"] = new_name
                        if e.get("target") == old:
                            e["target"] = new_name
                    applied = {**raw, "name": old}

            elif op == "add_edge":
                src = _resolve_task("add_edge", raw.get("source"), tasks, agents, notes=notes)["name"]
                tgt = _resolve_task("add_edge", raw.get("target"), tasks, agents, notes=notes)["name"]
                if src == tgt:
                    raise ChatError("add_edge: a node cannot feed itself")
                existing = next((e for e in edges if e.get("source") == src and e.get("target") == tgt), None)
                if existing is not None and 'mappings' not in raw and 'control_only' not in raw:
                    continue
                edge = {"source": src, "target": tgt}
                if raw.get('control_only'):
                    edge['control_only'] = True
                elif 'mappings' in raw:
                    edge['mappings'] = copy.deepcopy(raw['mappings'])
                else:
                    outputs = {p['name'] for p in _find(tasks, src).get('outputs', [])}
                    shared = [p['name'] for p in _find(tasks, tgt).get('inputs', []) if p['name'] in outputs]
                    if shared: edge['mappings'] = [{'from': name, 'to': name} for name in shared]
                    else: edge['control_only'] = True
                if existing is None: edges.append(edge)
                else: existing.clear(); existing.update(edge)
                applied = {**raw, "source": src, "target": tgt}

            elif op == "delete_edge":
                src = _resolve_task("delete_edge", raw.get("source"), tasks, agents,
                                    destructive=True, notes=notes)["name"]
                tgt = _resolve_task("delete_edge", raw.get("target"), tasks, agents,
                                    destructive=True, notes=notes)["name"]
                if not any(e.get("source") == src and e.get("target") == tgt for e in edges):
                    touching = [{"source": e.get("source"), "target": e.get("target")} for e in edges
                                if {e.get("source"), e.get("target")} & {src, tgt}]
                    raise ChatError(
                        f"delete_edge: there is no edge {src} -> {tgt}"
                        + (". Edges touching these nodes: "
                           + ", ".join(f"{e['source']} -> {e['target']}" for e in touching)
                           if touching else " and no edges touch these nodes"),
                        touching)
                graph["edges"] = [e for e in edges
                                  if not (e.get("source") == src and e.get("target") == tgt)]
                applied = {"op": "delete_edge", "source": src, "target": tgt}

            elif op == "set_goal":
                graph["goal"] = raw.get("goal") or ""
                applied = raw

            elif op == "set_name":
                if raw.get("name"):
                    graph["name"] = raw["name"]
                    applied = raw

            elif op == "auto_layout":
                graph_store.auto_layout(graph)
                applied = raw

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
                from backend.features.library.tool_verification import verify_saved
                observe("create_tool", {"name":spec["name"], "verification":verify_saved(spec["name"])})

            elif op == "verify_tool":
                from backend.features.library.tool_verification import verify_saved
                observe("verify_tool", verify_saved(raw.get("name") or ""))

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
                match = _resolve("inspect_node", raw.get("name"), tasks, agents, notes=notes)
                observe("inspect_node", match["ref"] if match["kind"] != "chat_agent" else
                        agent_ops.inspect(match["ref"], agent_ops_.resources))

            elif op == "configure_workflow":
                patch = raw.get('patch') or {}
                if not isinstance(patch, dict) or set(patch) - {'goal', 'preprocess', 'output_dir'}:
                    raise ChatError('configure_workflow: unsupported setting')
                graph.update(copy.deepcopy(patch))
                applied = raw

            elif op == "open_panel":
                panel = raw.get('panel')
                if panel not in ('run', 'runs', 'schedule', 'evolve', 'review', 'workspace'):
                    panel = None
                    raise ChatError('Unknown workflow panel')

            elif op == "plan_workflow":
                from backend.api import run_plan
                plan = run_plan.compile_plan(graph, start_at=raw.get('start_at'))
                values = raw.get('inputs') or {}
                if not isinstance(values, dict): raise ChatError('inputs must be an object')
                observe(op, {**plan, 'input_errors': run_plan.check_inputs(plan, values)})

            elif op in ('set_schedule', 'clear_schedule'):
                from backend.api import scheduler, run_plan
                if op == 'clear_schedule': observe(op, {'removed': scheduler.clear(graph_id)})
                else:
                    config = scheduler.validate(raw.get('schedule') or {})
                    plan = run_plan.compile_plan(graph)
                    errors = run_plan.check_inputs(plan, config['inputs'])
                    if errors: raise ChatError('; '.join(errors))
                    graph = _save_graph(graph_id, graph); graph_id = graph['id']; saved = True
                    observe(op, scheduler.set_schedule(graph, config))

            elif op in ('read_watch', 'start_watch', 'stop_watch'):
                from backend.api import watcher
                if op == 'read_watch': observe(op, watcher.graph_watch_status(graph_id))
                elif op == 'stop_watch': observe(op, {'stopped': watcher.stop_graph_watch(graph_id)})
                else:
                    errors = _validate(graph)
                    if errors: raise ChatError('; '.join(errors))
                    graph = _save_graph(graph_id, graph); graph_id = graph['id']; saved = True
                    observe(op, {'watchers': watcher.start_graph_watch(graph)})

            elif op == "read_schedule":
                from backend.api import scheduler
                observe(op, scheduler.status(graph_id))

            elif op in ('list_memories', 'read_memory'):
                from backend.api import memory_resources
                scoped = agent_ops.with_scope(graph)
                if op == 'list_memories': observe(op, memory_resources.catalog(scoped))
                else:
                    resource = memory_resources.resolve(scoped, raw.get('memory_id'))
                    limit = max(1, min(100, int(raw.get('limit', 20))))
                    offset = max(0, int(raw.get('offset', 0)))
                    observe(op, memory_resources.read(scoped, resource, raw.get('query') or '', limit, offset=offset))

            elif op == "cancel_run":
                from backend.api import runner
                wanted = raw.get('run_id')
                if not wanted:
                    wanted = next((r['run_id'] for r in runner.list_runs(graph_id=graph_id) if r['status'] == 'running'), None)
                run = runner.get_run(wanted) if wanted else None
                if not run or run.get('graph_id') not in history_ids(graph_id): raise ChatError('No matching run in this workflow')
                observe(op, runner.cancel_run(wanted))

            elif op in ('list_batches', 'read_batch', 'cancel_batch'):
                from backend.api import batch
                if op == 'list_batches': observe(op, batch.list_batches(graph_id=graph_id))
                else:
                    candidate = batch.get_batch(raw.get('batch_id', ''))
                    if not candidate or candidate.get('graph_id') not in history_ids(graph_id): raise ChatError('No matching batch in this workflow')
                    observe(op, _truncate(candidate, 10000) if op == 'read_batch' else batch.cancel_batch(candidate['batch_id']))

            elif op == "list_runs":
                from backend.api import runner
                observe("list_runs", [
                    {"run_id": r.get("run_id"), "status": r.get("status"),
                     "created_at": r.get("created_at")}
                    for r in runner.list_runs(graph_id=graph_id)[:10]
                ])

            elif op == "read_run":
                from backend.api import runner
                wanted = raw.get("run_id")
                if not wanted:
                    recent = runner.list_runs(graph_id=graph_id)
                    if not recent:
                        raise ChatError("read_run: this workflow has no runs yet")
                    wanted = recent[0].get("run_id")
                run = runner.get_run(wanted)
                if run is None or run.get("graph_id") not in history_ids(graph_id):
                    raise ChatError(f"read_run: no run '{wanted}'")
                observe("read_run", _run_digest(run))

            elif op == "list_files":
                from backend.api import workspace as workspace_mod
                if not graph_id:
                    raise ChatError("list_files: no workflow is open")
                tree = workspace_mod.tree(graph_id)
                prefix = raw.get("path")
                if prefix:
                    tree = [f for f in tree if str(f.get("path", "")).startswith(prefix)]
                observe("list_files", tree[:200])

            elif op == "read_file":
                from backend.api import workspace as workspace_mod
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
                graph = _save_graph(graph_id, graph)
                graph_id = graph["id"]
                saved = True

            elif op == "set_output_dir":
                graph["output_dir"] = (raw.get("path") or "runs").strip() or "runs"
                applied = raw

            elif op == "write_file":
                from backend.api import workspace as workspace_mod
                if not graph_id:
                    raise ChatError("write_file: no workflow is open")
                workspace_mod.write_file(graph_id, raw.get("path") or "",
                                         str(raw.get("content") or "").encode("utf-8"))
                notes.append(f"Wrote {raw.get('path')}")

            elif op == "delete_file":
                from backend.api import workspace as workspace_mod
                if not graph_id:
                    raise ChatError("delete_file: no workflow is open")
                workspace_mod.delete_file(graph_id, raw.get("path") or "")
                notes.append(f"Deleted {raw.get('path')}")

            elif op == "make_dir":
                from backend.api import workspace as workspace_mod
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
                from backend.api import run_plan
                values = raw.get('inputs') or {}
                if not isinstance(values, dict): raise ChatError('inputs must be an object')
                plan = run_plan.compile_plan(graph, start_at=raw.get('start_at'))
                pending_run = {'inputs': values, 'start_at': plan['start_at'], 'plan_id': plan['plan_id'],
                               'plan': plan, 'input_errors': run_plan.check_inputs(plan, values),
                               'session': raw.get('session'), 'record': raw.get('record')}

            else:
                notes.append(f"Ignored unknown operation {op!r}")
            if applied is not None:
                canvas_operations.append(copy.deepcopy(applied))
        except custom_tools.CustomToolError as e:
            notes.append(f"create_tool failed: {e}")
        except skills_api.SkillError as e:
            notes.append(f"{op} failed: {e}")
        except ChatError as e:
            notes.append(str(e))
            observe('operation_error', {'operation': op, 'error': str(e),
                                        **({'candidates': e.candidates} if e.candidates else {})})
        except Exception as e:  # one bad operation must not lose the others
            notes.append(f"{op} failed: {type(e).__name__}: {e}")
            observe("operation_error", {"operation": op, "error": str(e)})

    return {
        "graph": graph,
        "canvas_agents": agents,
        "agent_actions": agent_actions,
        "canvas_operations": canvas_operations,
        "notes": notes,
        "tools_created": tools_created,
        "skills_created": skills_created,
        "observations": observations,
        "saved": saved,
        "pending_run": pending_run,
        "panel": panel,
    }


# --------------------------------------------------------------------------
# endpoint
# --------------------------------------------------------------------------

def _ask(messages: list) -> str:
    if chat_control.current.get():
        try:
            return chat_control.worker('model', {'messages': messages})
        except Exception as exc:
            raise ChatError(str(exc)) from exc
    from llm import ProviderError
    from llm import chat as llm_chat

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


# A request to change something, in either of the languages Studio's users
# write in. Used only to decide whether a reply with no operations deserves
# one reminder and a visible "nothing changed" -- never to pick an operation.
_EDIT_WORDS = re.compile(
    r"\b(delete|remove|drop|add|insert|create|rename|connect|disconnect|link|wire|"
    r"update|change|modify|replace|set|move|attach|detach)\b"
    r"|删|移除|去掉|去除|添加|增加|新增|加上|创建|新建|改|重命名|连接|连上|断开|更新|替换|设置|移动",
    re.I)


def looks_like_edit(message: str) -> bool:
    text = (message or "").strip()
    if not text or text.endswith(("?", "？")):
        return False
    return bool(_EDIT_WORDS.search(text))


@router.post("/graphs/{graph_id}/chat/replay")
def replay(graph_id: str, body: dict = Body(...)):
    """Apply a settled turn's canvas edits to the canvas as it is now.

    A turn works on the canvas sent with the message; if the person edited the
    canvas while it ran, replacing the canvas with the turn's result would
    throw their edits away. The client sends the current canvas and the turn's
    `canvas_operations` instead. Only canvas edits are replayed: everything
    else the turn did (files, tools, saves, Chat Agents) already happened.
    """
    if not graph_store.graph_exists(graph_id):
        raise HTTPException(status_code=404, detail=f"Graph '{graph_id}' not found")
    graph = dict(body.get("graph") or {})
    graph.pop("canvas_agents", None)
    operations = [o for o in (body.get("operations") or [])
                  if isinstance(o, dict) and o.get("op") in CANVAS_OPS]
    result = apply_operations(graph, operations, graph_id, agents=[])
    errors = _validate(result["graph"]) if operations else []
    failed = [o for o in result["observations"] if o.get("op") == "operation_error"]
    return {"graph": graph if errors else result["graph"],
            "applied": not errors and not failed,
            "errors": errors, "notes": result["notes"]}


@router.post("/graphs/{graph_id}/chat")
def chat(graph_id: str, body: dict = Body(...)):
    if not graph_store.graph_exists(graph_id):
        raise HTTPException(status_code=404, detail=f"Graph '{graph_id}' not found")

    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=422, detail="message is required")

    # The client sends the canvas it is looking at, which may hold unsaved
    # edits; falling back to the stored graph only if it sent nothing.
    graph = dict(body.get("graph") or graph_store.load_graph(graph_id) or {})
    # Chat Agents the canvas shows. They are not part of the workflow graph
    # (never saved into it); the assistant sees them to address them.
    agents = graph.pop("canvas_agents", None) or []
    history = [m for m in (body.get("history") or [])
               if isinstance(m, dict) and m.get("role") in ("user", "assistant")]

    engine = ChatEngine(_ask, _build_system_prompt(graph, agents), history, message)
    messages = engine.messages
    try:
        answer = engine.ask()
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
            # The model's reply is written before anything was built; say
            # what is actually happening, or "created" reads as done.
            "notes": ["Generating the workflow in the background; it appears on "
                      "the canvas when it is done (usually a few minutes)."]
                     + ([] if len(operations) == 1 else
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
    agent_actions: list[dict] = []
    canvas_operations: list[dict] = []
    tools_created: list[str] = []
    skills_created: list[str] = []
    steps: list[dict] = []
    saved = False
    pending_run = None
    panel = None
    activity = []

    def execute(operations):
        nonlocal updated, graph_id, notes, saved, pending_run, panel, agents
        result = apply_operations(updated, operations, graph_id, agents=agents)
        updated = result["graph"]
        agents = result["canvas_agents"]
        agent_actions.extend(result["agent_actions"])
        canvas_operations.extend(result["canvas_operations"])
        remaining = [o for o in operations if isinstance(o, dict)]
        for observation in result["observations"]:
            index = next((i for i, o in enumerate(remaining) if o.get('op') == observation.get('op')), None)
            operation = remaining.pop(index) if index is not None else {'op': observation.get('op')}
            activity.append({'operation': operation, 'result': observation.get('result')})
        if result["saved"]:
            graph_id = updated["id"]
        notes += result["notes"]
        tools_created.extend(result["tools_created"])
        skills_created.extend(result["skills_created"])
        saved = saved or result["saved"]
        pending_run = result["pending_run"] or pending_run
        panel = result.get("panel") or panel
        return {"observations": result["observations"], "stop": bool(pending_run),
                "canvas": _canvas_summary(updated, agents)}

    original_agents = copy.deepcopy(agents)
    expects_edit = looks_like_edit(message)
    try:
        answer = engine.run(execute, initial=answer, max_steps=MAX_STEPS, expects_edit=expects_edit)
        reply = str(answer.get("reply") or reply).strip()
    except (ChatError, ValueError) as exc:
        notes.append(str(exc))
    steps = engine.turns

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
                retry = apply_operations(graph, retry_ops, graph_id, agents=original_agents)
                retry_errors = _validate(retry["graph"])
                if not retry_errors:
                    updated, errors = retry["graph"], []
                    notes = notes + retry["notes"]
                    all_operations = retry_ops
                    agent_actions[:] = retry["agent_actions"]
                    canvas_operations[:] = retry["canvas_operations"]
                    tools_created += retry["tools_created"]
                    skills_created += retry["skills_created"]
                    saved = saved or retry["saved"]
                    reply = str(retry_answer.get("reply") or reply).strip()
        except (ChatError, ValueError):
            pass  # keep the first attempt's errors for the user to see

    applied = not errors and json.dumps(updated, sort_keys=True) != json.dumps(graph, sort_keys=True)
    if expects_edit and not applied and not errors and not agent_actions and not saved and not pending_run:
        notes.append("No changes were applied to the canvas.")
    return {
        "reply": reply or ("Done." if all_operations else ""),
        "operations": all_operations,
        # The canvas edits that took effect, names resolved: what the client
        # replays if the person changed the canvas while this turn ran.
        "canvas_operations": [] if errors else canvas_operations,
        # Chat Agent changes for the client to carry out; independent of the
        # workflow graph, so a graph that fails validation does not hold them.
        "agent_actions": agent_actions,
        # `graph` is the canvas to show; on failure it is the unchanged input,
        # so a rejected turn never half-applies.
        "graph": graph if errors else updated,
        "applied": applied,
        "errors": errors,
        "notes": notes,
        "tools_created": tools_created,
        "skills_created": skills_created,
        "saved": saved and not errors,
        "pending_run": None if errors else pending_run,
        "panel": None if errors else panel,
        "steps": len(steps),
        "activity": activity,
    }


# --------------------------------------------------------------------------
# generation jobs
# --------------------------------------------------------------------------

def _reap_jobs() -> None:
    cutoff = time.time() - JOB_TTL
    with _jobs_lock:
        for job_id in [k for k, v in _jobs.items() if v["updated_at"] < cutoff]:
            _jobs.pop(job_id, None)
            _job_controls.pop(job_id, None)


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

    control = chat_control.current.get()
    if control:
        control.background = True
        _job_controls[job_id] = control
    def work():
        context_token = chat_control.current.set(control)
        try:
            stage = lambda text: _set_job(job_id, stage=text)
            generated = chat_control.worker('generate', {'goal': goal}, stage) if control else _workflow_from_goal(goal, on_stage=stage)
            chat_control.check()
            _set_job(job_id, draft_graph=copy.deepcopy(generated))
            # The framework emits legacy name-based dependencies. Normalize
            # that fresh graph before merging into a current-version canvas;
            # migrating the parent would skip the new bare edges entirely.
            generated = graph_store.migrate_flow(copy.deepcopy(generated))
            result = copy.deepcopy(graph)
            result["flow_version"] = generated["flow_version"]
            result.pop("migration_warnings", None)
            if generated.get("migration_warnings"):
                result["migration_warnings"] = generated["migration_warnings"]
            result["goal"] = goal
            result["tasks"] = generated["tasks"]
            result["edges"] = generated["edges"]
            graph_store.auto_layout(result)
            errors = _validate(result)
            if errors:
                _set_job(job_id, status="failed",
                         draft_graph=result, error="The generated workflow did not validate: "
                               + "; ".join(errors))
            else:
                _set_job(job_id, status="done", stage="Done", graph=result, draft_graph=None)
        except chat_control.Cancelled:
            _set_job(job_id, status="cancelled", stage="Stopped", graph=None, draft_graph=None, error=None)
        except Exception as e:
            _set_job(job_id, status="failed", error=f"{type(e).__name__}: {e}")
        finally:
            if control:
                control.finished = True
                control.updated = time.monotonic()
            chat_control.current.reset(context_token)

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
    if job.get("graph_id") != graph_id: raise HTTPException(404, detail="Generation job not found for this workflow")
    job["elapsed"] = int(time.time() - job["started_at"])
    return job


@router.post("/graphs/{graph_id}/assistant/{request_id}/stop")
def stop_assistant(graph_id: str, request_id: str):
    try:
        control = chat_control.get_control(graph_id, request_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    control.event.set()
    # A server-side turn settles here rather than when its thread unwinds: the
    # poller must not re-attach to it, and its answer is no longer wanted.
    turn = turn_jobs.stop(graph_id, request_id)
    if turn is not None:
        return {"status": turn["status"], "turn": turn}
    return {"status": "stopped" if control.finished else "stopping"}


@router.post("/graphs/{graph_id}/chat/jobs/{job_id}/stop")
def stop_generation(graph_id: str, job_id: str):
    job = generation_job(graph_id, job_id)
    if job['status'] != 'running':
        return {"status": job['status']}
    control = _job_controls.get(job_id)
    if not control:
        raise HTTPException(409, 'This older generation cannot be interrupted; wait for it to finish.')
    control.event.set()
    return {"status": "stopping"}


def _dispatch(graph_id: str, body: dict):
    context = body.get("context", "canvas")
    if context == "canvas":
        return chat(graph_id, body)
    if context == "results":
        from backend.api.result_chat import chat_results
        return chat_results(graph_id, body)
    raise HTTPException(422, "Unknown assistant context")


@router.post("/graphs/{graph_id}/assistant")
def assistant(graph_id: str, body: dict = Body(...)):
    if body.get('background'):
        return _start_turn(graph_id, body)
    control = None
    token = None
    if body.get('request_id'):
        try:
            control = chat_control.get_control(graph_id, body['request_id'], start=True)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        token = chat_control.current.set(control)
    try:
        chat_control.check()
        return _dispatch(graph_id, body)
    except chat_control.Cancelled:
        return {"stopped": True, "reply": "Stopped.", "operations": [], "activity": []}
    finally:
        if control and not control.background:
            control.finished = True
            control.updated = time.monotonic()
        if token is not None:
            chat_control.current.reset(token)


# --------------------------------------------------------------------------
# background assistant turns
# --------------------------------------------------------------------------

def _start_turn(graph_id: str, body: dict):
    """Start a turn that outlives this request; the client polls its outcome.

    The turn is keyed by its request id, so the existing Stop endpoint cancels
    it; leaving the chat (closing the connection) does not.
    """
    context = body.get("context", "canvas")
    if context not in ("canvas", "results"):
        raise HTTPException(422, "Unknown assistant context")
    request_id = body.get('request_id') or uuid.uuid4().hex
    try:
        control = chat_control.get_control(graph_id, request_id, start=True)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    payload = {k: v for k, v in body.items() if k != 'background'}
    return turn_jobs.start(graph_id, request_id, context, lambda: _dispatch(graph_id, payload), control)


@router.get("/graphs/{graph_id}/assistant/turns")
def running_turns(graph_id: str, context: str | None = None):
    """Turns still running for this workflow, so a remounted chat re-attaches."""
    return {"turns": turn_jobs.running(graph_id, context)}


@router.get("/graphs/{graph_id}/assistant/turns/{turn_id}")
def assistant_turn(graph_id: str, turn_id: str):
    return turn_jobs.get(graph_id, turn_id)
