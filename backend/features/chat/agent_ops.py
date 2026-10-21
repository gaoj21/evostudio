"""The workflow assistant's operations on Chat Agents.

A Chat Agent is a workflow node of kind "chat_agent" (see
backend/features/agents/chat_agents.py), so every operation here is an edit
of the graph: it changes the node in the canvas the turn works on and comes
back as an ordinary canvas operation (add_node / update_node / rename_node),
which the client replays and undoes like any other edit. What it writes is
checked first, against the same rules the harness applies (AgentSettings,
the memory catalog and validate_memories, the tool and skill catalogs), so a
bad request comes back to the model as an operation_error naming what it
could have meant.

A memory connection is the node's `memories` binding list -- the canvas's own
connect gesture edits exactly that, and its "reads"/"writes" edges are drawn
from it -- so connecting or disconnecting a memory is an update of that list.
"""

from __future__ import annotations

import copy
import re

from backend.features.agents import chat_agents
from backend.features.chat import node_resolver

# Settings an operation may set; "skills" is accepted for skill_names.
FIELDS = ("name", "description", "instructions", "provider", "max_steps", "timeout", "toolkits",
          "tools", "skill_names", "memories", "x", "y")
ALIASES = {"skills": "skill_names"}


def _error(message, candidates=None):
    from backend.features.chat.chat_api import ChatError
    return ChatError(message, candidates)


# --------------------------------------------------------------------------
# catalogs
# --------------------------------------------------------------------------

def with_scope(graph: dict) -> dict:
    """The canvas as the chat receives it, with the fields that decide which
    shared memories it can reach. The canvas snapshot carries neither
    project_id nor task_id; without them a workflow in a project looked for
    its shared memories under its own id, found none, and nothing on the
    canvas could be connected. The stored workflow is the authority."""
    if graph.get("project_id") and graph.get("task_id"):
        return graph
    try:
        from backend.api import graphs
        stored = graphs.load_graph(graph.get("id")) if graph.get("id") else None
    except Exception:  # noqa: BLE001 - an unsaved canvas keeps what it has
        stored = None
    if not stored:
        return graph
    return {**graph, **{key: stored[key] for key in ("project_id", "task_id")
                        if stored.get(key) and not graph.get(key)}}


def memory_catalog(graph: dict) -> list:
    """The memories this canvas can connect a Chat Agent to: its shared (Mem0)
    spaces in scope and its workflow memories, as the canvas shows them."""
    try:
        from backend.api import memory_resources
        return memory_resources.catalog(with_scope(graph))
    except Exception:  # noqa: BLE001 - a canvas without scope has no memories
        return []


def binding_key(binding: dict) -> str:
    return binding.get("memory_id") or "mem:space:" + str(binding.get("space_id") or "")


def _memory_candidates(resources):
    return [node_resolver.candidate(r["id"], "memory", r, labels=[r.get("name")],
                                    names=[r.get("space_id")], description=r.get("kind"))
            for r in resources]


def resolve_memory(op: str, query, resources: list, label="Memories this canvas can connect") -> dict:
    if isinstance(query, dict):
        query = query.get("memory_id") or query.get("space_id") or query.get("memory") or query.get("name")
    if not resources:
        raise _error(f"{op}: this canvas has no memory to connect. Add a shared memory first "
                     "(Library > Shared memory), or enable long-term memory on a workflow node.")
    result = node_resolver.resolve(query, _memory_candidates(resources))
    if result["status"] != "ok":
        reachable = ", ".join(f"{r['name']} ({r['id']})" for r in resources)
        raise _error(node_resolver.failure(op, query, result).replace("on the canvas", "among the memories")
                     + f" {label}: {reachable}.", result["candidates"])
    return result["match"]["ref"]


def tool_catalog() -> list:
    from backend.api import tools_registry
    return [t for t in tools_registry.list_tools() if t.get("available")]


def _all_tools(catalog) -> list:
    return [sub["name"] for t in catalog for sub in t.get("tools", [])]


def resolve_tools(op: str, query, catalog: list) -> list:
    """Sub-tool names a query means: one tool, or every tool of a toolkit."""
    candidates = ([node_resolver.candidate(t["name"], "toolkit", t, description=t.get("description"))
                   for t in catalog]
                  + [node_resolver.candidate(sub["name"], "tool", {"tools": [sub]},
                                             description=sub.get("description") or t["name"])
                     for t in catalog for sub in t.get("tools", [])])
    result = node_resolver.resolve(query, candidates)
    if result["status"] != "ok":
        raise _error(node_resolver.failure(op, query, result).replace("on the canvas", "among the available tools"),
                     result["candidates"])
    return [sub["name"] for sub in result["match"]["ref"].get("tools", [])]


def skill_catalog() -> list:
    from backend.api import skills_api
    return skills_api.list_skills()


def resolve_skill(op: str, query, skills: list) -> str:
    candidates = [node_resolver.candidate(s["name"], "skill", s, description=s.get("description"))
                  for s in skills]
    result = node_resolver.resolve(query, candidates)
    if result["status"] != "ok":
        raise _error(node_resolver.failure(op, query, result).replace("on the canvas", "among the skills"),
                     result["candidates"])
    return result["match"]["id"]


# --------------------------------------------------------------------------
# views
# --------------------------------------------------------------------------

def _memory_view(binding, by_id):
    key = binding_key(binding)
    resource = by_id.get(key)
    return {"id": key, "name": resource["name"] if resource else None,
            **({"kind": resource["kind"]} if resource else {"unavailable": True}),
            "read": bool(binding.get("read", True)), "write": bool(binding.get("write"))}


def _tools_view(agent):
    if agent.get("tools") is None and agent.get("toolkits") is None:
        return "all available tools"
    if agent.get("tools") is None:
        return {"toolkits": agent.get("toolkits")}
    return agent.get("tools")


def summary(agent: dict, resources: list) -> dict:
    """A Chat Agent node as the assistant sees it on the canvas, every step."""
    by_id = {r["id"]: r for r in resources}
    instructions = str(agent.get("instructions") or "")
    out = {"name": agent.get("name"), "kind": chat_agents.KIND,
           "instructions": instructions[:160] + ("…" if len(instructions) > 160 else "")}
    if agent.get("description"):
        out["description"] = str(agent["description"])[:120]
    out["provider"] = agent.get("provider") or "platform default"
    out["tools"] = _tools_view(agent)
    if agent.get("skill_names"):
        out["skills"] = agent["skill_names"]
    out["memories"] = [_memory_view(m, by_id) for m in agent.get("memories") or []]
    return out


def inspect(agent: dict, resources: list) -> dict:
    by_id = {r["id"]: r for r in resources}
    full = {k: v for k, v in agent.items() if k != "memories"}
    full["provider"] = agent.get("provider") or None
    full["tools"] = _tools_view(agent)
    full["memories"] = [_memory_view(m, by_id) for m in agent.get("memories") or []]
    full["kind"] = chat_agents.KIND
    return full


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def _binding(op, item, resources, previous):
    """One memory binding from what the model wrote: a name/id or an object."""
    if isinstance(item, str):
        item = {"memory": item}
    if not isinstance(item, dict):
        raise _error(f"{op}: each memory must be a name, an id or an object")
    resource = resolve_memory(op, item, resources)
    old = next((m for m in previous if binding_key(m) == resource["id"]), {})
    binding = {k: v for k, v in {**old, **item}.items() if k.endswith("_handle") and isinstance(v, str)}
    binding.update({"space_id": resource["space_id"]} if resource["kind"] == "mem0"
                   else {"memory_id": resource["id"]})
    binding["read"] = bool(item.get("read", old.get("read", True)))
    binding["write"] = bool(item.get("write", old.get("write", False)))
    if binding["write"] and not resource.get("writable"):
        raise _error(f"{op}: '{resource['name']}' cannot be written by a Chat Agent: "
                     f"{resource.get('write_reason') or 'read-only'}. Connect it with read only.")
    if not (binding["read"] or binding["write"]):
        raise _error(f"{op}: a memory connection must read, write or both")
    return binding


def _tools(op, value, catalog):
    if value is None:
        return None
    if not isinstance(value, list):
        raise _error(f"{op}: tools must be a list of tool names, or null for all available tools")
    names = []
    for item in value:
        names += [n for n in resolve_tools(op, item, catalog) if n not in names]
    return names


def normalize_patch(op: str, patch, agent: dict, graph: dict, resources: list) -> dict:
    """The patch as the harness store accepts it, checked; raises ChatError."""
    if not isinstance(patch, dict):
        raise _error(f"{op}: patch must be an object of settings")
    patch = {ALIASES.get(k, k): v for k, v in patch.items()}
    ignored = [k for k in patch if k not in FIELDS]
    patch = {k: v for k, v in patch.items() if k in FIELDS}
    if "name" in patch:
        if not (isinstance(patch["name"], str) and 1 <= len(patch["name"].strip()) <= 120):
            raise _error(f"{op}: name must be 1-120 characters")
        patch["name"] = patch["name"].strip()
    if "instructions" in patch and not (isinstance(patch["instructions"], str)
                                        and len(patch["instructions"]) <= 30000):
        raise _error(f"{op}: instructions must be text of at most 30000 characters")
    if "provider" in patch:
        provider = patch["provider"]
        if provider is not None and not isinstance(provider, str):
            raise _error(f"{op}: provider must be a provider name, or null for the platform default")
        patch["provider"] = (provider or "").strip() or None
    if "tools" in patch or "toolkits" in patch:
        catalog = tool_catalog()
        if "tools" in patch:
            patch["tools"] = _tools(op, patch["tools"], catalog)
        if "toolkits" in patch and patch["toolkits"] is not None:
            if not isinstance(patch["toolkits"], list):
                raise _error(f"{op}: toolkits must be a list of toolkit names, or null")
            known = {t["name"] for t in catalog}
            unknown = [t for t in patch["toolkits"] if t not in known]
            if unknown:
                raise _error(f"{op}: unknown toolkit {', '.join(map(str, unknown))}. "
                             f"Available: {', '.join(sorted(known)) or '(none)'}")
    if "skill_names" in patch:
        if not isinstance(patch["skill_names"], list):
            raise _error(f"{op}: skill_names must be a list of skill names")
        skills = skill_catalog()
        names = []
        for item in patch["skill_names"]:
            name = resolve_skill(op, item, skills)
            if name not in names:
                names.append(name)
        patch["skill_names"] = names
    if "memories" in patch:
        if not isinstance(patch["memories"], list):
            raise _error(f"{op}: memories must be a list of memory names or bindings")
        previous = agent.get("memories") or []
        patch["memories"] = [_binding(op, m, resources, previous) for m in patch["memories"]]
        check_memories(op, graph, patch["memories"], resources)
    _check_settings(op, {**_settings(agent), **patch})
    if ignored:
        patch["_ignored"] = ignored
    return patch


def check_memories(op, graph, memories, resources):
    """The harness's own memory rule, as the save will apply it."""
    keys = [binding_key(m) for m in memories]
    if len(set(keys)) != len(keys):
        raise _error(f"{op}: the same memory is connected twice")
    from fastapi import HTTPException
    from backend.api import harness_api
    try:
        harness_api.validate_memories(with_scope(graph), {"memories": memories})
    except HTTPException as exc:
        reachable = ", ".join(f"{r['name']} ({r['id']})" for r in resources) or "(none)"
        raise _error(f"{op}: {exc.detail} Memories this canvas can connect: {reachable}.",
                     [{"id": r["id"], "name": r["name"], "kind": r["kind"]} for r in resources]) from exc


def _settings(agent):
    return {k: v for k, v in agent.items() if k in FIELDS or k == "engine"}


def _check_settings(op, settings):
    from pydantic import ValidationError
    from backend.api import harness_api
    try:
        harness_api.AgentSettings(**settings)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise _error(f"{op}: invalid Chat Agent settings ({problems})") from exc


# --------------------------------------------------------------------------
# operations
# --------------------------------------------------------------------------

class AgentOps:
    """Applies one batch's Chat Agent operations to the graph the turn works
    on (`get_graph()`, read each time: a save in the batch replaces it).
    Each change returns the canvas operations it amounts to."""

    def __init__(self, get_graph, notes, observe, resolve, rename):
        self.get_graph, self.notes, self.observe = get_graph, notes, observe
        self._resolve, self._rename = resolve, rename
        self._resources = None

    @property
    def tasks(self):
        return self.get_graph().setdefault("tasks", [])

    @property
    def agents(self):
        return [t for t in self.tasks if chat_agents.is_chat_agent(t)]

    @property
    def resources(self):
        if self._resources is None:
            self._resources = memory_catalog(self.get_graph())
        return self._resources

    def find(self, op, raw, destructive=False):
        query = raw.get("agent") or raw.get("name") or raw.get("id")
        if isinstance(query, str) and query.startswith("chat:"):
            query = query[len("chat:"):]
        return self._resolve(op, query, self.agents, destructive=destructive, notes=self.notes)

    def _record(self, op, match, patch, text) -> list:
        agent = match["ref"]
        ignored = patch.pop("_ignored", None)
        if ignored:
            self.notes.append(f"{op}: ignored unsupported field(s) {', '.join(ignored)}; "
                              f"supported: {', '.join(FIELDS)}")
        applied = []
        if "name" in patch:
            new_name = patch.pop("name")
            if new_name != agent.get("name") and chat_agents.node_name(new_name, set()) != agent.get("name"):
                applied.append(self._rename(agent, new_name))
        changed = {k: v for k, v in patch.items() if agent.get(k, object()) != v}
        if not changed and not applied:
            self.notes.append(f"Chat Agent '{agent.get('name')}' already has those settings; nothing changed.")
            return []
        if changed:
            agent.update(copy.deepcopy(changed))
            applied.append({"op": "update_node", "name": agent["name"], "patch": copy.deepcopy(changed)})
        applied[-1]["summary"] = text
        return applied

    def update(self, op, raw, patch=None, match=None) -> list:
        match = match or self.find(op, raw)
        agent = match["ref"]
        before = agent.get("name")
        patch = normalize_patch(op, raw.get("patch") if patch is None else patch, agent,
                                self.get_graph(), self.resources)
        if not {k for k in patch if k != "_ignored"}:
            raise _error(f"{op}: nothing to change; supported fields: {', '.join(FIELDS)}")
        fields = [k for k in patch if k != "_ignored"]
        if fields == ["name"]:
            text = f"renamed Chat Agent '{before}' to '{patch['name']}'"
        else:
            text = f"updated Chat Agent '{before}' ({', '.join(fields)})"
        return self._record(op, match, patch, text)

    def create(self, op, raw) -> list:
        spec = raw.get("agent") if isinstance(raw.get("agent"), dict) else raw.get("settings") or {
            k: v for k, v in raw.items() if k != "op"}
        if not isinstance(spec, dict):
            raise _error(f"{op}: give the new Chat Agent's settings as fields of the operation")
        wanted_id = spec.get("agent_id")
        spec = {k: v for k, v in spec.items() if k not in ("id", "engine", "agent_id", "kind")}
        taken = {t.get("name") for t in self.tasks}
        if not str(spec.get("name") or "").strip():
            n = len(self.agents) + 1
            while f"chat_agent_{n}" in taken:
                n += 1
            spec["name"] = f"chat_agent_{n}"
        settings = normalize_patch(op, spec, {}, self.get_graph(), self.resources)
        ignored = settings.pop("_ignored", None)
        if ignored:
            self.notes.append(f"{op}: ignored unsupported field(s) {', '.join(ignored)}")
        asked = settings.pop("name")
        name = chat_agents.node_name(asked, taken)
        if name != chat_agents.node_name(asked, set()):
            self.notes.append(f"{op}: named the new Chat Agent '{name}' ('{asked}' is taken)")
        count = len(self.agents)
        used = {a.get("agent_id") for a in self.agents}
        agent_id = (wanted_id if isinstance(wanted_id, str) and re.fullmatch(chat_agents.AGENT_ID[1:-1], wanted_id)
                    and wanted_id not in used else chat_agents.new_agent_id())
        task = chat_agents.normalize({"x": 400 + count * 40, "y": 250, **copy.deepcopy(settings),
                                      "name": name, "kind": chat_agents.KIND, "agent_id": agent_id})
        self.tasks.append(task)
        return [{"op": "add_node", "task": copy.deepcopy(task), "summary": f"created Chat Agent '{name}'"}]

    def connect(self, op, raw) -> list:
        match = self.find(op, raw)
        agent = match["ref"]
        resource = resolve_memory(op, raw.get("memory"), self.resources)
        current = copy.deepcopy(agent.get("memories") or [])
        old = next((m for m in current if binding_key(m) == resource["id"]), None)
        read = bool(raw.get("read", True)) or bool(old and old.get("read"))
        write = bool(raw.get("write", False)) or bool(old and old.get("write"))
        binding = _binding(op, {"memory": resource["id"], "read": read, "write": write}, self.resources, current)
        memories = [binding if binding_key(m) == resource["id"] else m for m in current] if old else current + [binding]
        check_memories(op, self.get_graph(), memories, self.resources)
        mode = "reads and writes" if read and write else "writes" if write else "reads"
        return self._record(op, match, {"memories": memories},
                            f"connected '{agent.get('name')}' to memory '{resource['name']}' ({mode})")

    def disconnect(self, op, raw) -> list:
        match = self.find(op, raw)
        agent = match["ref"]
        current = copy.deepcopy(agent.get("memories") or [])
        by_id = {r["id"]: r for r in self.resources}
        connected = [{**(by_id.get(binding_key(m)) or {"name": binding_key(m), "kind": "unavailable"}),
                      "id": binding_key(m)} for m in current]
        if not connected:
            raise _error(f"{op}: Chat Agent '{agent.get('name')}' is not connected to any memory")
        resource = resolve_memory(op, raw.get("memory"), connected, f"'{agent.get('name')}' is connected to")
        direction = raw.get("direction")
        memories = []
        for m in current:
            if binding_key(m) == resource["id"] and direction in ("read", "write"):
                m = {**m, direction: False}
            if binding_key(m) != resource["id"] or (direction in ("read", "write") and (m.get("read") or m.get("write"))):
                memories.append(m)
        return self._record(op, match, {"memories": memories},
                            f"disconnected '{agent.get('name')}' from memory '{resource['name']}'"
                            + (f" ({direction})" if direction in ("read", "write") else ""))

    def tool(self, op, raw, add) -> list:
        match = self.find(op, raw)
        agent = match["ref"]
        catalog = tool_catalog()
        names = resolve_tools(op, raw.get("tool"), catalog)
        everything = _all_tools(catalog)
        if agent.get("tools") is None and agent.get("toolkits") is None:
            if add:
                self.notes.append(f"Chat Agent '{agent.get('name')}' already has every available tool, "
                                  f"including {', '.join(names)}; nothing changed.")
                return []
            current = everything
        elif agent.get("tools") is None:
            current = [sub["name"] for t in catalog if t["name"] in agent["toolkits"] for sub in t.get("tools", [])]
        else:
            current = list(agent["tools"])
        tools = current + [n for n in names if n not in current] if add else [n for n in current if n not in names]
        label = ", ".join(names)
        text = (f"gave '{agent.get('name')}' the tool {label}" if add
                else f"removed the tool {label} from '{agent.get('name')}'")
        return self._record(op, match, {"tools": tools, "toolkits": None}, text)

    def skill(self, op, raw, add) -> list:
        match = self.find(op, raw)
        agent = match["ref"]
        current = list(agent.get("skill_names") or [])
        if add:
            name = resolve_skill(op, raw.get("skill"), skill_catalog())
            skills = current + ([name] if name not in current else [])
        else:
            name = resolve_skill(op, raw.get("skill"), [{"name": n} for n in current]) if current else None
            if name is None:
                raise _error(f"{op}: Chat Agent '{agent.get('name')}' has no skills")
            skills = [n for n in current if n != name]
        return self._record(op, match, {"skill_names": skills},
                            f"{'gave' if add else 'removed'} skill '{name}' {'to' if add else 'from'} '{agent.get('name')}'")

    def inspect(self, op, raw) -> list:
        self.observe(op, inspect(self.find(op, raw)["ref"], self.resources))
        return []

    def list(self, op) -> list:
        self.observe(op, [summary(a, self.resources) for a in self.agents])
        return []

    def apply(self, op, raw):
        """The canvas operations `op` amounts to, if it is a Chat Agent
        operation; None otherwise."""
        handlers = {
            "create_agent": lambda: self.create(op, raw),
            "update_agent": lambda: self.update(op, raw),
            "connect_agent_memory": lambda: self.connect(op, raw),
            "disconnect_agent_memory": lambda: self.disconnect(op, raw),
            "add_agent_tool": lambda: self.tool(op, raw, True),
            "remove_agent_tool": lambda: self.tool(op, raw, False),
            "add_agent_skill": lambda: self.skill(op, raw, True),
            "remove_agent_skill": lambda: self.skill(op, raw, False),
            "inspect_agent": lambda: self.inspect(op, raw),
            "list_agents": lambda: self.list(op),
        }
        if op not in handlers:
            return None
        return handlers[op]()


AGENT_OPS = ("create_agent", "update_agent", "delete_agent", "connect_agent_memory",
             "disconnect_agent_memory", "add_agent_tool", "remove_agent_tool",
             "add_agent_skill", "remove_agent_skill")
