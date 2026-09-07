"""Source-code and README templates for exported Studio projects."""

import pprint

from . import tools_registry


def _py(value) -> str:
    return pprint.pformat(value, width=88, sort_dicts=False)


def _toolkit_of(tool_name: str | None) -> str | None:
    """The toolkit that exports a tool, resolved while the registry is here."""
    if not tool_name:
        return None
    found = tools_registry.find_tool(tool_name)
    if found is None:
        return tool_name          # unknown: let the project report it by name
    kind, target = found
    return target["name"] if kind == "custom" else target


def _workflow_py(goal, llm_tasks, tool_nodes, edges, skills, project, local_modules) -> str:
    """The runnable heart of the export: tasks as data, execution as code."""
    framework_tasks = [
        {k: t[k] for k in ("name", "description", "inputs", "outputs", "prompt",
                           "system_prompt", "parse_mode", "tool_names", "skill_names",
                           "use_long_term_memory", "memory")
         if k in t and t[k] not in (None, [], "", False)}
        for t in llm_tasks
    ]
    # A tool node names a tool, and a tool lives in a toolkit. Which one is
    # known here and nowhere else: resolving it inside the exported project
    # would mean shipping the whole registry to look it up again.
    tool_specs = []
    for t in tool_nodes:
        spec = {k: t[k] for k in ("name", "tool", "inputs", "outputs") if k in t}
        spec["toolkit"] = _toolkit_of(t.get("tool"))
        tool_specs.append(spec)
    # Built outside the f-string: doubled braces inside an interpolated
    # expression are a set literal, not an escape.
    edge_pairs = [{"source": e.get("source"), "target": e.get("target")} for e in edges]
    name_hint = (goal.strip().rstrip('.') if goal else "") or "exported"
    return f'''"""The {name_hint} workflow, exported from EvoAgentX Studio.

Everything the workflow is lives in this file: the tasks below are the exact
node definitions from the canvas, and `run()` executes them the same way
Studio does. Edit the dicts to change the workflow.
"""

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# The framework and the provider/memory layers travel with this project.
VENDOR = HERE / "vendor"
if VENDOR.is_dir() and str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))

PROJECT = {project!r}
GOAL = {goal!r}
GRAPH_ID = {project!r}

# Toolkits Studio registered itself; their modules travel under vendor/.
LOCAL_TOOLKIT_MODULES = {local_modules!r}

# LLM tasks, in dependency order. `prompt` refers to its inputs with single
# braces; a task with parse_mode "json" must ask for JSON whose keys are its
# output names.
TASKS = {_py(framework_tasks)}

# Deterministic nodes: plain function calls, no LLM. They run before the LLM
# tasks and their results become inputs for them.
TOOL_NODES = {_py(tool_specs)}

EDGES = {_py(edge_pairs)}


def load_llm():
    """Build the LLM.

    Prefers the bundled provider layer (vendor/llm/providers.json), which is
    how the rest of this project selects a model — set EAX_PROVIDER to pick a
    non-default one. Falls back to a single endpoint from EAX_MODEL /
    EAX_API_KEY when that layer or its config is unavailable.
    """
    from evoagentx.models import LiteLLM, LiteLLMConfig

    if not os.environ.get("EAX_MODEL"):
        try:
            from llm import get_evoagentx_llm

            return get_evoagentx_llm(os.environ.get("EAX_PROVIDER") or None)
        except Exception as exc:
            print(f"[llm] provider layer unavailable ({{exc}}); using EAX_MODEL")

    model = os.environ.get("EAX_MODEL")
    api_key = os.environ.get("EAX_API_KEY")
    base_url = os.environ.get("EAX_BASE_URL")
    if not model or not api_key:
        raise SystemExit(
            "Set EAX_MODEL and EAX_API_KEY (copy .env.example to .env), or "
            "configure vendor/llm/providers.json. See the README."
        )
    if base_url:
        # An explicit endpoint goes through LiteLLM's openai-compatible path.
        config = LiteLLMConfig(
            model=model if model.startswith("openai/") else f"openai/{{model}}",
            api_key=api_key,
            api_base=base_url,
        )
    else:
        # LiteLLM wants the key under the provider's own field (deepseek_key,
        # anthropic_key, ...); only some providers fall back to a generic one.
        prefix = model.split("/")[0]
        field = f"{{prefix}}_key"
        if field not in LiteLLMConfig.model_fields:
            field = "api_key"
        config = LiteLLMConfig(model=model, **{{field: api_key}})
    return LiteLLM(config=config)


def load_skills(tasks):
    """Append each task's skills to its system prompt.

    A skill is standing instructions the task always follows (a taxonomy, a
    rubric, a house style). Studio resolves them at run time; here they are
    read from skills/<name>/SKILL.md so the exported project is self-contained.
    """
    out = []
    for task in tasks:
        names = task.get("skill_names") or []
        task = {{k: v for k, v in task.items() if k != "skill_names"}}
        blocks = []
        for name in names:
            path = HERE / "skills" / name / "SKILL.md"
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            body = text.split("---", 2)[-1].strip() if text.startswith("---") else text
            blocks.append(f"## Skill: {{name}}\\n\\n{{body}}")
        if blocks:
            base = (task.get("system_prompt") or "").strip()
            task["system_prompt"] = (base + "\\n\\n" if base else "") + "\\n\\n".join(blocks)
        out.append(task)
    return out


# Generated Tool classes, kept by the tool they came from. The framework
# registers every subclass in a process-wide registry keyed on the class name
# and refuses a second one, so building the toolkits twice in one process --
# two runs, a batch -- would otherwise raise "Found duplicate module".
_TOOL_CLASSES = {{}}


def build_toolkits(names):
    """Resolve toolkit names to instances: built-ins by import, custom from tools/."""
    import hashlib

    from evoagentx.tools.tool import Tool, Toolkit

    py_types = {{"string": str, "integer": int, "number": float,
                "boolean": bool, "object": dict, "array": list}}
    toolkits = []
    for name in names:
        spec_path = HERE / "tools" / f"{{name}}.json"
        if spec_path.is_file():
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            code = (HERE / "tools" / f"{{name}}.py").read_text(encoding="utf-8")
            ns = {{}}
            exec(compile(code, str(spec_path.with_suffix(".py")), "exec"), ns)
            # A toolkit is a module: every tool it declared is one of the
            # public functions in it.
            built = []
            for tool in spec.get("tools") or []:
                key = json.dumps(tool, sort_keys=True, default=str)
                if key in _TOOL_CLASSES:
                    built.append(_TOOL_CLASSES[key]())
                    continue
                fn = ns.get(tool["name"])
                if not callable(fn):
                    raise SystemExit(
                        f"tools/{{name}}.py does not define {{tool['name']}}(...)"
                    )
                params = tool.get("params") or []
                sig = ", ".join(f"{{p['name']}}: {{py_types[p['type']].__name__}}"
                                for p in params)
                call_kwargs = ", ".join(f"'{{p['name']}}': {{p['name']}}" for p in params)
                src = (f"def __call__(self{{', ' if sig else ''}}{{sig}}):\\n"
                       f"    return _fn(**{{{{{{call_kwargs}}}}}})\\n")
                cns = {{"_fn": fn}}
                exec(compile(src, "<tool>", "exec"), cns)
                # The class name only has to be unique in that registry; the
                # tool's own `name` is what the framework and the model use.
                cls_name = f"{{tool['name']}}_{{hashlib.sha1(key.encode()).hexdigest()[:8]}}"
                cls = type(cls_name, (Tool,), {{
                    "__annotations__": {{"name": str, "description": str,
                                        "inputs": dict, "required": list}},
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "inputs": {{p["name"]: {{"type": p["type"],
                                          "description": p.get("description", "")}}
                               for p in params}},
                    "required": [p["name"] for p in params],
                    "__call__": cns["__call__"],
                }})
                _TOOL_CLASSES[key] = cls
                built.append(cls())
            toolkits.append(Toolkit(name=name, tools=built))
            continue
        import importlib

        cls = getattr(importlib.import_module("evoagentx.tools"), name, None)
        for module_name in LOCAL_TOOLKIT_MODULES:
            if cls is not None:
                break
            try:
                cls = getattr(importlib.import_module(module_name), name, None)
            except Exception as exc:
                raise SystemExit(
                    f"Toolkit '{{name}}' lives in vendor/{{module_name}}.py and "
                    f"could not be imported: {{exc}}. See the README."
                ) from exc
        if cls is None:
            raise SystemExit(f"Unknown toolkit: {{name}}")
        toolkits.append(cls())
    return toolkits


def run_tool_nodes(inputs):
    """Run the deterministic nodes and merge their results into the inputs.

    Structured results are JSON-stringified because the framework passes task
    inputs as strings — the same thing Studio does.
    """
    merged = dict(inputs)
    for node in TOOL_NODES:
        # The node names a tool; the toolkit holding it was resolved at export.
        toolkit = build_toolkits([node.get("toolkit") or node["tool"]])[0]
        tool = (toolkit.get_tool(node["tool"]) if hasattr(toolkit, "get_tool")
                else toolkit.get_tools()[0])
        if tool is None:
            raise SystemExit(
                f"Toolkit '{{toolkit.name}}' has no tool '{{node['tool']}}'"
            )
        args = {{}}
        for p in node.get("inputs") or []:
            if p["name"] in merged:
                args[p["name"]] = merged[p["name"]]
            elif p.get("required", True):
                raise SystemExit(f"Tool node '{{node['name']}}' needs input '{{p['name']}}'")
        result = tool(**args)
        out_name = ((node.get("outputs") or [{{}}])[0]).get("name") or "result"
        merged[out_name] = (result if isinstance(result, str)
                            else json.dumps(result, ensure_ascii=False, default=str))
        print(f"[tool] {{node['name']}} -> {{out_name}}")
    return merged


MEMORY_DIR = HERE / "memory_store"


def _table_store():
    """The vendored table store, pointed at this project rather than at the
    Studio checkout it was copied from."""
    import table_store

    table_store.TABLES_DIR = HERE / "memory_tables"
    return table_store


def prepare_ltm(tasks):
    """Open a long-term memory store per task that asked for one.

    Only opens them. Each node searches its own store at the moment it runs
    (attach_ltm), which is the first point its real inputs exist: a node fed by
    an upstream node is not described by the workflow's inputs at all.
    A memory failure degrades the run rather than stopping it.
    """
    import memory_policy

    memories = {{}}
    for task in tasks:
        if not task.get("use_long_term_memory"):
            continue
        name = task["name"]
        # A table node keeps rows and never searches a corpus; opening one
        # would load an embedding model to build an index nothing reads.
        if memory_policy.policy(task)["kind"] == "table":
            continue
        try:
            # The layer picks the backend from EAX_MEMORY_BACKEND; importing a
            # backend module directly would pin it to one.
            from memory import open_memory

            memories[name] = open_memory(
                MEMORY_DIR / name, f"{{PROJECT}}-{{name}}", create=True
            )
        except Exception as exc:
            print(f"[memory] {{name}}: {{exc}} — continuing without it")

    # A node may read another node's memory. That store is opened read-only,
    # so naming it does not bring one into existence for a node that keeps
    # nothing of its own.
    import memory_policy

    for task in tasks:
        if not task.get("use_long_term_memory"):
            continue
        for other in (memory_policy.stores_read_by(task) or []):
            if other in memories:
                continue
            try:
                from memory import open_memory

                opened = open_memory(
                    MEMORY_DIR / other, f"{{PROJECT}}-{{other}}", create=False
                )
                if opened is not None:
                    memories[other] = opened
            except Exception as exc:
                print(f"[memory] {{other}}: {{exc}} — continuing without it")
    return memories


def agent_for_node(manager, node):
    """The agent the framework built for a node.

    It is not named after the task ("judge" becomes "JudgeAgent"), so the
    node's own record of its agent is the only reliable way across.
    """
    wanted = next((a.get("name") for a in (node.agents or []) if a.get("name")), None)
    return next((a for a in (manager.agents or []) if a.name == wanted), None)


def list_memory_entries(node):
    """Every entry in one node's store, for the by-subject recall.

    Not a search: two runs about the same obligor are about the same obligor
    whether or not their text is alike.
    """
    from memory import list_entries

    try:
        return list_entries(MEMORY_DIR / node)
    except Exception:
        return []


def recall_before_running(agent, task, stores, run_data=None):
    """Make a node search its memory when it runs, with the inputs it was given.

    The prompt is a template filled from those inputs, and they only exist once
    everything upstream has finished — so the recall is spliced in for that one
    call and taken out again afterwards.
    """
    import memory_policy
    from evoagentx.actions.customize_action import CustomizeAction

    action = next((a for a in (agent.actions or []) if isinstance(a, CustomizeAction)), None)
    if action is None or action.prompt is None:
        return
    base_prompt = action.prompt
    original = action.async_execute

    async def execute_with_recall(*args, inputs=None, **kwargs):
        block = ""
        try:
            block = await memory_policy.recall_async(
                stores, task, inputs or {{}},
                history=lambda agent: list_memory_entries(agent),
                # Includes source-node outputs, so a node dated by a field it
                # never takes as an input can still be held to that date.
                run_data=run_data,
                table=_table_store(), graph_id=GRAPH_ID,
            )
        except Exception as exc:
            print(f"[memory] {{task.get('name')}}: {{exc}} — running without recall")
        # The prompt goes through str.format(), so braces in the recalled
        # content have to survive as literals.
        action.prompt = base_prompt + block.replace("{{", "{{{{").replace("}}", "}}}}")
        try:
            return await original(*args, inputs=inputs, **kwargs)
        finally:
            action.prompt = base_prompt

    action.async_execute = execute_with_recall


def attach_ltm(manager, graph, tasks, memories, run_data=None):
    """Give each memory-enabled node its store and its recall."""
    by_name = {{t["name"]: t for t in tasks}}
    for node in graph.nodes:
        memory = memories.get(node.name)
        task = by_name.get(node.name, {{"name": node.name}})
        if memory is None and not keeps_table(task):
            continue
        agent = agent_for_node(manager, node)
        if agent is None:
            print(f"[memory] no agent for node {{node.name}} — it will store nothing")
            continue
        if memory is not None:
            agent.use_long_term_memory = True
            agent.storage_handler = memory.storage_handler
            agent.long_term_memory = memory
        recall_before_running(agent, by_name.get(node.name, {{"name": node.name}}),
                              memories, run_data=run_data)


def keeps_table(task):
    """Does this node keep a table rather than a searchable corpus?"""
    import memory_policy

    return bool(task.get("use_long_term_memory")) and \
        memory_policy.policy(task)["kind"] == "table"


def save_ltm(memories, graph, workflow, succeeded=True):
    """Write what each node chose to keep into its store.

    Nothing else writes to it: the framework holds the store but does not add
    to it, so without this the project would retrieve from a memory that stays
    empty for ever.
    """
    # Table-backed nodes intentionally have no vector store in `memories`.
    # Do not skip their writes when the exported workflow uses tables only.
    if not memories and not any(keeps_table(task) for task in TASKS):
        return
    import memory_policy
    from evoagentx.core.message import Message, MessageType

    try:
        data = workflow.environment.get_all_execution_data()
    except Exception:
        data = {{}}
    by_name = {{t["name"]: t for t in TASKS}}
    for node in graph.nodes:
        memory = memories.get(node.name)
        task = by_name.get(node.name, {{"name": node.name}})
        if memory is None and not keeps_table(task):
            continue
        try:
            payload = memory_policy.select(
                task,
                {{p.name: data[p.name] for p in (node.inputs or []) if p.name in data}},
                {{p.name: data[p.name] for p in (node.outputs or []) if p.name in data}},
                succeeded=succeeded,
                run_data=data,
            )
            if payload is None:
                continue
            def as_message(body):
                return Message(
                    content=json.dumps(body, ensure_ascii=False, default=str),
                    msg_type=MessageType.RESPONSE,
                    agent=node.name,
                    wf_goal=GOAL,
                    wf_task=node.name,
                    wf_task_desc=task.get("description", ""),
                )

            # A table node writes a row and is done: the primary key does
            # what a read-modify-write under a lock used to do, and badly.
            row = memory_policy.table_write(task, payload)
            if row is not None:
                import datetime

                _table_store().upsert(
                    GRAPH_ID, node.name, row["subject"], row["at"], row["payload"],
                    datetime.datetime.now(datetime.timezone.utc).isoformat())
                continue
            # Missing table key means there is no row to write; it does not
            # turn a table node into a vector-memory node.
            if keeps_table(task):
                print(f"[memory] {{node.name}}: no table subject — nothing stored")
                continue

            # A node that tracks a subject keeps one entry per subject — that
            # obligor's whole history — rather than one per run.
            subject = memory_policy.subject_of(task, payload)
            if subject is None:
                memory.add([as_message(payload)])
            else:
                dated_by = memory_policy.policy(task)["at"]
                mine, ids = [], []
                for entry in list_memory_entries(node.name):
                    body = memory_policy._unwrap(entry.get("content"))
                    if not isinstance(body, dict):
                        continue
                    if memory_policy.subject_of(task, body) != subject:
                        continue
                    mine.append(body)
                    if entry.get("memory_id"):
                        ids.append(entry["memory_id"])
                memory.add([as_message(memory_policy.merge_timeline(
                    mine, memory_policy.as_timeline(task, payload), dated_by))])
                if ids:
                    # After the merged entry is in, never before: a failure
                    # here leaves a duplicate, which recall folds back together
                    # by date, where the other order would lose the history.
                    try:
                        memory.delete(ids)
                    except Exception:
                        pass
            memory.save()
        except Exception as exc:
            print(f"[memory] {{node.name}}: {{exc}} — the run itself is unaffected")


def run(inputs):
    """Execute the workflow and return its result."""
    from evoagentx.agents.agent_manager import AgentManager
    from evoagentx.workflow.workflow import WorkFlow
    from evoagentx.workflow.workflow_graph import SequentialWorkFlowGraph

    llm = load_llm()
    inputs = run_tool_nodes(inputs)
    tasks = load_skills(TASKS)
    memories = prepare_ltm(tasks)
    framework_tasks = [{{k: v for k, v in t.items()
                        if k not in ("use_long_term_memory", "memory")}}
                       for t in tasks]
    tool_names = sorted({{n for t in tasks for n in (t.get("tool_names") or [])}})
    graph = SequentialWorkFlowGraph(goal=GOAL, tasks=framework_tasks)
    manager = AgentManager()
    manager.add_agents_from_workflow(
        graph, llm_config=llm.config, tools=build_toolkits(tool_names) or None
    )
    attach_ltm(manager, graph, tasks, memories, run_data=inputs)
    workflow = WorkFlow(graph=graph, agent_manager=manager, llm=llm)
    result = workflow.execute(inputs=inputs)
    succeeded = getattr(result, "status", "success") == "success"
    save_ltm(memories, graph, workflow, succeeded=succeeded)
    return result.result if hasattr(result, "result") else result
'''


def _run_py(name, required_inputs) -> str:
    flags = "\n".join(
        f'    parser.add_argument("--{i["name"]}", help={(i.get("description") or i["name"])!r})'
        for i in required_inputs
    ) or "    # this workflow declares no external inputs"
    return f'''#!/usr/bin/env python3
"""Command line entry point for the {name} workflow.

    python run.py --inputs data/sample_input.json
    python run.py --topic "quantum computing"      # per-input flags also work

Results are printed and written to out/<timestamp>.json.
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass  # dotenv is optional; environment variables work either way

import workflow

HERE = Path(__file__).resolve().parent


def parse_args():
    parser = argparse.ArgumentParser(description={name!r})
    parser.add_argument("--inputs", help="JSON file holding all inputs")
    parser.add_argument("--out", default="out", help="directory for results")
{flags}
    return parser.parse_args()


def main():
    args = parse_args()
    inputs = {{}}
    if args.inputs:
        inputs.update(json.loads(Path(args.inputs).read_text(encoding="utf-8")))
    # Flags override anything from the file.
    for key, value in vars(args).items():
        if key not in ("inputs", "out") and value is not None:
            inputs[key] = value

    print(f"running with inputs: {{list(inputs)}}")
    result = workflow.run(inputs)

    out_dir = HERE / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{{datetime.now().strftime('%Y%m%d_%H%M%S')}}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"\\nwritten to {{path.relative_to(HERE)}}")


if __name__ == "__main__":
    main()
'''


def _readme(name, goal, llm_tasks, tool_nodes, source_nodes, edges,
            required_inputs, builtin_tools, custom_tools_list, skills, sample, model,
            local_tools=None, local_tool_report=None) -> str:
    lines = [f"# {name}", ""]
    if goal:
        lines += [goal, ""]
    lines += [
        "Exported from EvoAgentX Studio as a deployable project: the agent "
        "framework, the provider layer and the memory layer travel with it "
        "under `vendor/`, so it needs no Studio, no server and no checkout of "
        "the original repository.",
        "",
        "## Quick start",
        "",
        "```bash",
        "pip install -r requirements.txt   # third-party deps only",
        "cp .env.example .env              # then put your API key in it",
        "python run.py --inputs data/sample_input.json",
        "```",
        "",
        "`vendor/evoagentx` is this project's own copy of the framework, not the "
        "PyPI release — it carries local changes the workflow was built against, "
        "so replacing it with `pip install evoagentx` can change behaviour.",
        "",
        "## Inputs",
        "",
    ]
    if required_inputs:
        lines += ["| name | type | required | description |", "|---|---|---|---|"]
        for i in required_inputs:
            lines.append(
                f"| `{i['name']}` | {i.get('type', 'str')} | "
                f"{'yes' if i.get('required', True) else 'no'} | {i.get('description', '')} |"
            )
        lines += [
            "",
            "Pass them in a JSON file (`--inputs`) or as flags "
            f"(`--{required_inputs[0]['name']} ...`).",
        ]
    else:
        lines.append("This workflow takes no external inputs.")
    lines += ["", "## Pipeline", ""]
    if tool_nodes:
        lines += ["Deterministic nodes, run first:", ""]
        for t in tool_nodes:
            outs = ", ".join(o.get("name", "") for o in t.get("outputs") or [])
            lines.append(f"- **{t['name']}** — calls `{t.get('tool')}` → `{outs}`")
        lines.append("")
    lines += ["LLM tasks, in order:", ""]
    for t in llm_tasks:
        ins = ", ".join(f"`{i['name']}`" for i in t.get("inputs") or []) or "—"
        outs = ", ".join(f"`{o['name']}`" for o in t.get("outputs") or []) or "—"
        lines.append(f"- **{t['name']}** — {t.get('description', '').strip()}")
        lines.append(f"  - in: {ins} · out: {outs} · parse: `{t.get('parse_mode', 'str')}`")
        if t.get("tool_names"):
            lines.append(f"  - tools: {', '.join(t['tool_names'])}")
        if t.get("skill_names"):
            lines.append(f"  - skills: {', '.join(t['skill_names'])}")
    lines += ["", "Edges (execution order):", "", "```"]
    lines += [f"{e.get('source')} → {e.get('target')}" for e in edges] or ["(single task)"]
    lines += ["```", ""]

    if custom_tools_list or builtin_tools:
        lines += ["## Tools", ""]
        for t in builtin_tools:
            lines.append(f"- `{t}` — built into evoagentx, imported at run time.")
        for spec in custom_tools_list:
            lines.append(f"- `{spec['name']}` — {spec['description']} "
                         f"(source in `tools/{spec['name']}.py`)")
        lines.append("")
    if local_tools:
        lines += [
            "### External toolkits", "",
            "These were registered by Studio rather than shipped with evoagentx. "
            "Their source is bundled under `tools/_local/`, but they were written "
            "against this project's layout and may import modules or data files "
            "that are not in this export:", "",
        ]
        report = local_tool_report or {}
        for module_name in local_tools:
            missing = report.get(module_name) or []
            if missing:
                mods = ", ".join(f"`{m}`" for m in missing)
                lines.append(
                    f"- `tools/_local/{module_name}.py` — **imports {mods}, which "
                    "is not in this export.** Any task using this toolkit will "
                    "fail at that point."
                )
            else:
                lines.append(f"- `tools/_local/{module_name}.py`")
        lines += [
            "",
            "To make one work: install or copy the module it names next to this "
            "project (its data files too), or remove the toolkit from the task "
            "that uses it and let that task do the work in its prompt.", "",
        ]
    if skills:
        lines += ["## Skills", "",
                  "Standing instructions appended to a task's system prompt at run time.", ""]
        for s in skills:
            lines.append(f"- `{s['name']}` — {s['description']} "
                         f"(`skills/{s['name']}/SKILL.md`)")
        lines.append("")

    lines += ["## Layout", "", "```",
              "run.py                CLI entry point",
              "workflow.py           the tasks and how they execute",
              "graph.json            the canvas graph this was exported from",
              "manifest.json         where this bundle came from (commit, date)",
              "tools/                custom tool code + specs",
              "skills/               SKILL.md instruction packs",
              "data/                 sample input (and output, if a run existed)",
              "memory_store/         long-term memory, written as runs happen",
              "vendor/               evoagentx, the llm and memory layers, and",
              "                      any project modules the toolkits import",
              "```", "",
              "`run.py` puts `vendor/` on `sys.path` before anything else, so "
              "the bundled copies win over anything installed system-wide.", ""]

    if source_nodes:
        lines += [
            "## Note on source nodes", "",
            "On the canvas this workflow started from "
            f"{', '.join('`' + s['name'] + '`' for s in source_nodes)}, which "
            "fetched data inside Studio (news feeds, dataset samples). Those "
            "feeds are not part of this export, so **their outputs are now "
            "inputs you supply** — they are listed in the Inputs table above. "
            "`data/sample_input.json` holds a real set of values captured from "
            "an actual run, so the workflow is runnable as shipped.", "",
        ]
    ltm_tasks = [t["name"] for t in llm_tasks if t.get("use_long_term_memory")]
    lines += ["## Model and memory", "",
              "The model comes from `vendor/llm/providers.json` (set "
              "`EAX_PROVIDER` to choose one, keys via `.env`). Setting "
              "`EAX_MODEL` + `EAX_API_KEY` instead bypasses that layer and "
              "talks to a single endpoint.", ""]
    if ltm_tasks:
        lines += [
            "Long-term memory is **on** for "
            + ", ".join(f"`{n}`" for n in ltm_tasks)
            + ". Each keeps its own store under `memory_store/<task>/`, written "
            "as runs happen — the first run starts empty, later runs retrieve "
            "what earlier ones learned. Delete the folder to reset it.", "",
            "The memory layer has two interchangeable backends, chosen by "
            "`EAX_MEMORY_BACKEND`: `framework` (the default — evoagentx "
            "LongTermMemory over FAISS + SQLite) and `langchain` "
            "(langchain-community FAISS + langchain-huggingface embeddings). "
            "Both use the same embedding model, but **their on-disk formats "
            "are not interchangeable**: switching backends starts from an "
            "empty store. `requirements.txt` lists the packages each one "
            "needs.", "",
        ]
    else:
        lines += ["No task uses long-term memory.", ""]
    lines += [
        "## Differences from Studio", "",
        "- Human review (HITL) is not included; the workflow runs end to end.",
        "- Runs are not recorded as Studio run artifacts; `run.py` writes the "
        "result to `out/<timestamp>.json` instead.",
        "- Everything else — task prompts, tool nodes, skills, long-term "
        "memory, execution order — is the same code path Studio uses.", "",
    ]
    if sample:
        lines += [f"`data/sample_input.json` came from run `{sample['run_id']}`.", ""]
    if model:
        lines += [f"Exported against model `{model}`; change it in `.env`.", ""]
    return "\n".join(lines)

