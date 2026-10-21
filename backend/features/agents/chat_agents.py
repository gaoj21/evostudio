"""Chat Agents as workflow nodes.

A Chat Agent is a task of kind "chat_agent" in the workflow graph: saved,
undone, copied, renamed, exported and edited with every other node. It does
not take part in runs -- it has no data edges and every run, plan, export and
Evolve sees the workflow without it (`workflow_only`) -- and it is chatted
with on its own. Its conversations live in the harness store, keyed by the
workflow's harness scope and the node's `agent_id`, which never changes when
the node is renamed.
"""
from __future__ import annotations

import copy
import uuid

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

KIND = "chat_agent"
AGENT_ID = r"^[a-f0-9]{32}$"
# The settings a Chat Agent node carries besides its name, kind and agent_id.
SETTINGS = ("description", "instructions", "provider", "max_steps", "timeout", "toolkits",
            "tools", "skill_names", "memories", "x", "y")
DEFAULT_INSTRUCTIONS = "You are a helpful research assistant."


class MemoryBinding(BaseModel):
    space_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')
    memory_id: str | None = Field(default=None, min_length=5, max_length=200)

    @model_validator(mode='after')
    def reference(self):
        if bool(self.space_id) == bool(self.memory_id): raise ValueError('Provide exactly one memory reference')
        return self
    read_source_handle: str | None = Field(default=None, max_length=40)
    read_target_handle: str | None = Field(default=None, max_length=40)
    write_source_handle: str | None = Field(default=None, max_length=40)
    write_target_handle: str | None = Field(default=None, max_length=40)
    read: bool = True
    write: bool = False


class ChatAgentNode(BaseModel):
    """A chat_agent task as it is stored. Unknown fields are dropped."""
    model_config = ConfigDict(extra='ignore')
    name: str = Field(min_length=1, max_length=120)
    kind: str = KIND
    agent_id: str | None = Field(default=None, pattern=AGENT_ID)
    description: str = Field(default='', max_length=2000)
    instructions: str = Field(default=DEFAULT_INSTRUCTIONS, max_length=30000)
    provider: str | None = None
    max_steps: int = Field(default=20, ge=1, le=100)
    timeout: int = Field(default=300, ge=10, le=1800)
    toolkits: list[str] | None = None
    tools: list[str] | None = None  # None = all; [] = no platform tools.
    skill_names: list[str] = Field(default_factory=list, max_length=100)
    memories: list[MemoryBinding] = Field(default_factory=list, max_length=100)
    x: float = Field(default=400, allow_inf_nan=False)
    y: float = Field(default=250, allow_inf_nan=False)


def is_chat_agent(task) -> bool:
    return isinstance(task, dict) and task.get("kind") == KIND


def new_agent_id() -> str:
    return uuid.uuid4().hex


def _invalid(task, exc: ValidationError) -> str:
    problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
    return f"Chat Agent '{task.get('name')}' has invalid settings ({problems})"


def normalize(task: dict) -> dict:
    """The stored form of a chat_agent task; raises ValueError naming what
    is wrong. Memory references are checked when the Agent is chatted with,
    where the workflow's scope is known (harness_api.validate_memories)."""
    try:
        node = ChatAgentNode(**task)
    except ValidationError as exc:
        raise ValueError(_invalid(task, exc)) from None
    out = node.model_dump()
    out["kind"] = KIND
    for key in ("x", "y"):
        if float(out[key]).is_integer():
            out[key] = int(out[key])
    # Unset references and handles are left out, as the canvas writes them.
    out["memories"] = [{k: v for k, v in m.items() if v is not None} for m in out["memories"]]
    return out


def normalize_tasks(tasks: list) -> list:
    """Every chat_agent task normalized, each with an agent_id of its own:
    a node without one (added by hand or by the assistant) or sharing one
    with an earlier node (a duplicate) gets a new one, so two nodes never
    share conversations. Raises ValueError for invalid settings."""
    seen = set()
    out = []
    for task in tasks or []:
        if is_chat_agent(task):
            task = normalize(task)
            if not task.get("agent_id") or task["agent_id"] in seen:
                task["agent_id"] = new_agent_id()
            seen.add(task["agent_id"])
        out.append(task)
    return out


def check_edges(tasks: list, edges: list) -> list[str]:
    """Errors for edges touching a Chat Agent: it takes no part in runs, so
    nothing flows into or out of it. Its memory connections are its
    `memories`, not edges."""
    names = {t.get("name") for t in tasks or [] if is_chat_agent(t)}
    return [f"Chat Agent '{n}' does not take part in runs and cannot have data edges "
            f"({e.get('source')} -> {e.get('target')}). Connect it to memories instead."
            for e in edges or [] if isinstance(e, dict)
            for n in [e.get("source") if e.get("source") in names else e.get("target")]
            if n in names]


def workflow_only(graph: dict) -> dict:
    """The graph as runs, plans, exports and Evolve see it: without Chat
    Agents (and any edge that touches one)."""
    tasks = graph.get("tasks") or []
    names = {t.get("name") for t in tasks if is_chat_agent(t)}
    if not names:
        return graph
    return {**graph, "tasks": [t for t in tasks if not is_chat_agent(t)],
            "edges": [e for e in graph.get("edges") or []
                      if e.get("source") not in names and e.get("target") not in names]}


def agents_of(graph: dict) -> list:
    return [t for t in (graph or {}).get("tasks") or [] if is_chat_agent(t)]


def find(graph: dict, agent_id: str) -> dict | None:
    return next((t for t in agents_of(graph) if t.get("agent_id") == agent_id), None)


def settings_of(task: dict) -> dict:
    """What the harness runs a turn with: the node's settings, `id` being
    its agent_id and `name` its node name."""
    task = normalize(task) if task.get("kind") == KIND else task
    return {"id": task.get("agent_id"), "name": task.get("name"),
            **{k: copy.deepcopy(task.get(k)) for k in SETTINGS}}


def space_of(binding) -> str | None:
    """The Mem0 space a binding names, or None for a workflow memory."""
    if binding.get('space_id'): return binding['space_id']
    reference = str(binding.get('memory_id') or '')
    return reference.removeprefix('mem:space:') if reference.startswith('mem:space:') else None


def node_name(base: str, taken: set) -> str:
    """A node name for `base`, as the canvas makes one (sluggify + suffix)."""
    import re
    from backend.features.persistence import name_tag
    slug = re.sub(r"[^a-z0-9]+", "_", str(base or "").lower()).strip("_")
    tag = name_tag(base)
    slug = (f"{slug}_{tag}" if slug else f"node_{tag}") if tag else (slug or "chat_agent")
    if slug not in taken:
        return slug
    i = 2
    while f"{slug}_{i}" in taken:
        i += 1
    return f"{slug}_{i}"
