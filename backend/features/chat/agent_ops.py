"""The workflow assistant's operations on canvas Chat Agents.

Chat Agents live in the harness store and the frontend owns their canvas
state, so nothing here writes the store: every change comes back as an
`agent_actions` entry the frontend carries out through the same calls its own
controls use (create / save / remove), which keeps its Undo and the canvas in
step. What can be checked without writing is checked here, against the same
rules the harness endpoints apply (AgentSettings, the memory catalog and
validate_memories, the tool and skill catalogs), so a bad request comes back
to the model as an operation_error naming what it could have meant, instead
of failing later in the browser.

A memory connection is the Agent's `memories` binding list -- the canvas's own
connect gesture edits exactly that, and its "reads"/"writes" edges are drawn
from it -- so connecting or disconnecting a memory is an update of that list.
"""

from __future__ import annotations

import copy
import uuid

from backend.features.chat import node_resolver

# Settings an operation may set; "skills" is accepted for skill_names.
FIELDS = ("name", "instructions", "provider", "max_steps", "timeout", "toolkits", "tools",
          "skill_names", "memories", "x", "y")
ALIASES = {"skills": "skill_names"}


def _error(message, candidates=None):
    from backend.features.chat.chat_api import ChatError
    return ChatError(message, candidates)


def bare_id(agent: dict) -> str:
    raw = str(agent.get("id") or "")
    return raw[len("chat:"):] if raw.startswith("chat:") else raw


# --------------------------------------------------------------------------
# catalogs
# --------------------------------------------------------------------------

def memory_catalog(graph: dict) -> list:
    """The memories this canvas can connect a Chat Agent to: its shared (Mem0)
    spaces in scope and its workflow memories, as the canvas shows them."""
    try:
        from backend.api import memory_resources
        return memory_resources.catalog(graph)
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
    """An Agent as the assistant sees it on the canvas, every step."""
    by_id = {r["id"]: r for r in resources}
    instructions = str(agent.get("instructions") or "")
    out = {"id": "chat:" + bare_id(agent), "name": agent.get("name") or bare_id(agent),
           "instructions": instructions[:160] + ("…" if len(instructions) > 160 else "")}
    if "provider" in agent:
        out["provider"] = agent.get("provider") or "platform default"
    if "tools" in agent or "toolkits" in agent:
        out["tools"] = _tools_view(agent)
    if agent.get("skill_names"):
        out["skills"] = agent["skill_names"]
    if "memories" in agent:
        out["memories"] = [_memory_view(m, by_id) for m in agent.get("memories") or []]
    return out


def inspect(agent: dict, resources: list) -> dict:
    by_id = {r["id"]: r for r in resources}
    full = {k: v for k, v in agent.items() if k != "memories"}
    full["id"] = "chat:" + bare_id(agent)
    full["provider"] = agent.get("provider") or None
    full["tools"] = _tools_view(agent)
    full["memories"] = [_memory_view(m, by_id) for m in agent.get("memories") or []]
    full["kind"] = "chat_agent"
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
    binding = {k: v for k, v in old.items() if k.endswith("_handle")}
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
        harness_api.validate_memories(graph, {"memories": memories})
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
    """Applies one batch's Chat Agent operations to `agents` (the list the
    assistant sees), recording an action per change for the frontend."""

    def __init__(self, graph, agents, actions, notes, observe, resolve):
        self.graph, self.agents, self.actions = graph, agents, actions
        self.notes, self.observe, self._resolve = notes, observe, resolve
        self._resources = None

    @property
    def resources(self):
        if self._resources is None:
            self._resources = memory_catalog(self.graph)
        return self._resources

    def find(self, op, raw, destructive=False):
        query = raw.get("agent") or raw.get("name") or raw.get("id")
        return self._resolve(op, query, [], self.agents, destructive=destructive, notes=self.notes)

    def _record(self, op, match, patch, text):
        agent = match["ref"]
        ignored = patch.pop("_ignored", None)
        if ignored:
            self.notes.append(f"{op}: ignored unsupported field(s) {', '.join(ignored)}; "
                              f"supported: {', '.join(FIELDS)}")
        changed = {k: v for k, v in patch.items() if agent.get(k, object()) != v}
        if not changed:
            self.notes.append(f"Chat Agent '{agent.get('name')}' already has those settings; nothing changed.")
            return
        agent.update(copy.deepcopy(changed))
        self.actions.append({"action": "update", "id": bare_id(agent), "patch": changed, "summary": text})

    def update(self, op, raw, patch=None, match=None):
        match = match or self.find(op, raw)
        agent = match["ref"]
        before = agent.get("name")
        patch = normalize_patch(op, raw.get("patch") if patch is None else patch, agent, self.graph, self.resources)
        if not {k for k in patch if k != "_ignored"}:
            raise _error(f"{op}: nothing to change; supported fields: {', '.join(FIELDS)}")
        fields = [k for k in patch if k != "_ignored"]
        if fields == ["name"]:
            text = f"renamed Chat Agent '{before}' to '{patch['name']}'"
        else:
            text = f"updated Chat Agent '{patch.get('name', before)}' ({', '.join(fields)})"
        self._record(op, match, patch, text)

    def create(self, op, raw):
        spec = raw.get("agent") if isinstance(raw.get("agent"), dict) else raw.get("settings") or {
            k: v for k, v in raw.items() if k != "op"}
        if not isinstance(spec, dict):
            raise _error(f"{op}: give the new Chat Agent's settings as fields of the operation")
        spec = {k: v for k, v in spec.items() if k not in ("id", "engine")}
        taken = {str(a.get("name") or "").casefold() for a in self.agents}
        if not str(spec.get("name") or "").strip():
            n = len(self.agents) + 1
            while f"chat agent {n}" in taken:
                n += 1
            spec["name"] = f"Chat Agent {n}"
        settings = normalize_patch(op, spec, {}, self.graph, self.resources)
        ignored = settings.pop("_ignored", None)
        if ignored:
            self.notes.append(f"{op}: ignored unsupported field(s) {', '.join(ignored)}")
        base, n = settings["name"], 2
        while settings["name"].casefold() in taken:
            settings["name"] = f"{base} {n}"
            n += 1
        if settings["name"] != base:
            self.notes.append(f"{op}: named the new Chat Agent '{settings['name']}' ('{base}' is taken)")
        # The id is chosen here so later operations of this turn can address
        # the new Agent before the frontend has created it.
        agent = {"id": uuid.uuid4().hex, **copy.deepcopy(settings)}
        self.agents.append(agent)
        self.actions.append({"action": "create", "id": agent["id"], "name": settings["name"],
                             "settings": settings, "summary": f"created Chat Agent '{settings['name']}'"})

    def connect(self, op, raw):
        match = self.find(op, raw)
        agent = match["ref"]
        resource = resolve_memory(op, raw.get("memory"), self.resources)
        current = copy.deepcopy(agent.get("memories") or [])
        old = next((m for m in current if binding_key(m) == resource["id"]), None)
        read = bool(raw.get("read", True)) or bool(old and old.get("read"))
        write = bool(raw.get("write", False)) or bool(old and old.get("write"))
        binding = _binding(op, {"memory": resource["id"], "read": read, "write": write}, self.resources, current)
        memories = [binding if binding_key(m) == resource["id"] else m for m in current] if old else current + [binding]
        check_memories(op, self.graph, memories, self.resources)
        mode = "reads and writes" if read and write else "writes" if write else "reads"
        self._record(op, match, {"memories": memories},
                     f"connected '{agent.get('name')}' to memory '{resource['name']}' ({mode})")

    def disconnect(self, op, raw):
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
        self._record(op, match, {"memories": memories},
                     f"disconnected '{agent.get('name')}' from memory '{resource['name']}'"
                     + (f" ({direction})" if direction in ("read", "write") else ""))

    def tool(self, op, raw, add):
        match = self.find(op, raw)
        agent = match["ref"]
        catalog = tool_catalog()
        names = resolve_tools(op, raw.get("tool"), catalog)
        everything = _all_tools(catalog)
        if agent.get("tools") is None and agent.get("toolkits") is None:
            if add:
                self.notes.append(f"Chat Agent '{agent.get('name')}' already has every available tool, "
                                  f"including {', '.join(names)}; nothing changed.")
                return
            current = everything
        elif agent.get("tools") is None:
            current = [sub["name"] for t in catalog if t["name"] in agent["toolkits"] for sub in t.get("tools", [])]
        else:
            current = list(agent["tools"])
        tools = current + [n for n in names if n not in current] if add else [n for n in current if n not in names]
        label = ", ".join(names)
        text = (f"gave '{agent.get('name')}' the tool {label}" if add
                else f"removed the tool {label} from '{agent.get('name')}'")
        self._record(op, match, {"tools": tools, "toolkits": None}, text)

    def skill(self, op, raw, add):
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
        self._record(op, match, {"skill_names": skills},
                     f"{'gave' if add else 'removed'} skill '{name}' {'to' if add else 'from'} '{agent.get('name')}'")

    def inspect(self, op, raw):
        self.observe(op, inspect(self.find(op, raw)["ref"], self.resources))

    def list(self, op):
        self.observe(op, [summary(a, self.resources) for a in self.agents])

    def apply(self, op, raw) -> bool:
        """Handles `op` if it is a Chat Agent operation; False otherwise."""
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
            return False
        handlers[op]()
        return True


AGENT_OPS = ("create_agent", "update_agent", "delete_agent", "connect_agent_memory",
             "disconnect_agent_memory", "add_agent_tool", "remove_agent_tool",
             "add_agent_skill", "remove_agent_skill")
