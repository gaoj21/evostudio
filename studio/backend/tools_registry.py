"""Tool registry for EvoAgentX Studio.

Lists the framework toolkits that are usable from canvas tasks and
instantiates them for runs. Only zero-config toolkits are enabled; toolkits
needing API keys or heavy dependencies (browsers, docker, databases, MCP) are
either listed with a `requires` marker (instantiation refused with a clear
error until the env vars exist) or omitted entirely.

Verified by probing import + no-arg instantiation of every enabled entry.
"""

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


class ToolResolveError(Exception):
    """User-facing tool resolution error (surfaced in the run error)."""


def _skill_toolkit(**kwargs):
    from evoagentx.tools.skill_tool import SkillToolkit

    import skills_api

    return SkillToolkit(skill_paths=str(skills_api.SKILLS_DIR))


def _make(module: str, class_name: str):
    def factory(**kwargs):
        import importlib

        cls = getattr(importlib.import_module(module), class_name)
        return cls(**kwargs)

    return factory


# name -> {factory, description, requires}
# `requires` lists env vars that must be set before the toolkit can be used.
# `storage`: toolkit accepts a storage_handler — runs pass one rooted at the
# graph's workspace files/ dir.
TOOL_REGISTRY: dict[str, dict] = {
    "FileToolkit": {
        "factory": _make("evoagentx.tools.file_tool", "FileToolkit"),
        "description": "Read, write and append files (read_file, write_file, append_file). Paths come from the LLM and are NOT redirected to the workspace.",
        "requires": [],
    },
    "StorageToolkit": {
        "factory": _make("evoagentx.tools.storage_file", "StorageToolkit"),
        "description": "File storage operations (save, read, append, delete, move, copy, create_directory, list_files, exists). Rooted at the graph workspace files/ dir during runs.",
        "requires": [],
        "storage": True,
    },
    "CMDToolkit": {
        "factory": _make("evoagentx.tools.cmd_toolkit", "CMDToolkit"),
        "description": "Execute shell commands (execute_command). Storage rooted at the graph workspace files/ dir during runs.",
        "requires": [],
        "storage": True,
    },
    "PythonInterpreterToolkit": {
        "factory": _make("evoagentx.tools.interpreter_python", "PythonInterpreterToolkit"),
        "description": "Execute Python code snippets or script files (python_execute, python_execute_script). Storage rooted at the graph workspace files/ dir during runs.",
        "requires": [],
        "storage": True,
    },
    "ArxivToolkit": {
        "factory": _make("evoagentx.tools.request_arxiv", "ArxivToolkit"),
        "description": "Search and download arXiv papers (arxiv_search, arxiv_download).",
        "requires": [],
    },
    "RequestToolkit": {
        "factory": _make("evoagentx.tools.request", "RequestToolkit"),
        "description": "Generic HTTP requests (http_request).",
        "requires": [],
    },
    "WikipediaSearchToolkit": {
        "factory": _make("evoagentx.tools.search_wiki", "WikipediaSearchToolkit"),
        "description": "Search Wikipedia (wikipedia_search).",
        "requires": [],
    },
    "DDGSSearchToolkit": {
        "factory": _make("evoagentx.tools.search_ddgs", "DDGSSearchToolkit"),
        "description": "DuckDuckGo web search (ddgs_search).",
        "requires": [],
    },
    "RSSToolkit": {
        "factory": _make("evoagentx.tools.rss_feed", "RSSToolkit"),
        "description": "Fetch and validate RSS feeds (rss_fetch, rss_validate).",
        "requires": [],
    },
    "ObligorMatchToolkit": {
        "factory": _make("obligor_tool", "ObligorMatchToolkit"),
        "description": "Match company names / SEC CIKs against the internal credit-risk obligor list (match_company_name, match_cik).",
        "requires": [],
    },
    # --- listed but unavailable until credentials exist ---------------------
    "GoogleSearchToolkit": {
        "factory": _make("evoagentx.tools.search_google", "GoogleSearchToolkit"),
        "description": "Google web search (google_search).",
        "requires": ["GOOGLE_API_KEY", "GOOGLE_SEARCH_ENGINE_ID"],
    },
    "SerperAPIToolkit": {
        "factory": _make("evoagentx.tools.search_serperapi", "SerperAPIToolkit"),
        "description": "Serper API web search.",
        "requires": ["SERPERAPI_KEY"],
    },
    "ExaSearchToolkit": {
        "factory": _make("evoagentx.tools.search_exa", "ExaSearchToolkit"),
        "description": "Exa neural web search.",
        "requires": ["EXA_API_KEY"],
    },
    # Skills as a tool: lets a node discover and load skills on demand instead
    # of having them pasted into its system prompt. Attach skills directly to a
    # node when it should always follow them; use this when the node should
    # choose which skill applies.
    "SkillToolkit": {
        "factory": _skill_toolkit,
        "description": "Discover and load Studio skills on demand (list_skills, load_skill).",
        "requires": [],
    },
}


def builtin_names() -> list[str]:
    """All built-in names: toolkit names AND their sub-tool names (a custom
    tool colliding with either would break tool resolution)."""
    names = set(TOOL_REGISTRY)
    for entry in TOOL_REGISTRY.values():
        if entry["requires"]:
            continue  # gated toolkits can't instantiate without keys
        try:
            names.update(entry["factory"]().get_tool_names())
        except Exception:
            continue
    return sorted(names)


def _sub_tools(toolkit) -> list[dict]:
    """Sub-tool schemas of a toolkit instance: name/description/inputs."""
    out = []
    for tool in toolkit.get_tools():
        out.append({
            "name": tool.name,
            "description": tool.description,
            "inputs": tool.inputs,
            "required": tool.required or [],
        })
    return out


def find_tool(name: str):
    """Locate a sub-tool by name. Returns ("custom", spec) for user-defined
    tools, ("builtin", toolkit_name) for built-ins, None otherwise."""
    import custom_tools

    found = custom_tools.find(name)
    if found is not None:
        return ("custom", found[0])
    for toolkit_name, entry in TOOL_REGISTRY.items():
        if entry["requires"]:
            continue
        if any(var for var in entry["requires"] if not os.getenv(var)):
            continue
        try:
            tk = entry["factory"]()
        except Exception:
            continue
        if name in tk.get_tool_names():
            return ("builtin", toolkit_name)
    return None


def call_tool(name: str, args: dict, workspace_dir=None):
    """Execute one tool (custom or built-in sub-tool) with args; returns the
    raw result (custom tools return {"result": ...}; built-ins return their
    own payload). Raises ToolResolveError / tool exceptions on failure."""
    import custom_tools

    kind, target = find_tool(name) or (None, None)
    if kind is None:
        raise ToolResolveError(f"Unknown tool '{name}'")
    if kind == "custom":
        out = custom_tools.run_custom_tool(name, args)
        if "error" in out:
            raise ToolResolveError(out["error"])
        return out.get("result")
    toolkit = resolve_tools([target], workspace_dir=workspace_dir)[0]
    tool = toolkit.get_tool(name)
    result = tool(**args)
    # tools may return ToolResult or a raw value
    return getattr(result, "result", result)


def list_tools() -> list[dict]:
    """Catalog for the frontend: name, description, sub-tool names, requires,
    and whether the toolkit is usable right now."""
    catalog = []
    for name, entry in TOOL_REGISTRY.items():
        missing = [var for var in entry["requires"] if not os.getenv(var)]
        item = {
            "name": name,
            "description": entry["description"],
            "requires": entry["requires"],
            "available": not missing,
        }
        if missing:
            item["unavailable_reason"] = f"Missing env vars: {', '.join(missing)}"
            item["tools"] = []
        else:
            try:
                # sub-tool granularity: name + inputs schema (drives the
                # draggable tool-node palette and the tool-node Inspector)
                item["tools"] = _sub_tools(entry["factory"]())
            except Exception as e:
                item["available"] = False
                item["unavailable_reason"] = f"{type(e).__name__}: {e}"
                item["tools"] = []
        catalog.append(item)
    # user-defined tools (studio/data/tools/), always available
    import custom_tools

    for spec in custom_tools.list_custom_tools():
        tools = custom_tools.tools_of(spec)
        catalog.append({
            "name": spec["name"],
            "description": spec["description"] + " (custom)",
            # A custom toolkit is a module, and every public function in it is
            # one tool — the same shape a built-in toolkit has.
            "tools": [{
                "name": tool["name"],
                "description": tool["description"],
                "inputs": {p["name"]: {"type": p["type"],
                                       "description": p.get("description", "")}
                           for p in tool.get("params") or []},
                "required": [p["name"] for p in tool.get("params") or []],
            } for tool in tools],
            "requires": [],
            "available": True,
            "custom": True,
        })
    return catalog


def validate_tool_names(tool_names: list[str]) -> None:
    """Check names and required env vars without instantiating anything."""
    import custom_tools

    custom = {t["name"] for t in custom_tools.list_custom_tools()}
    for name in tool_names or []:
        if name in custom:
            continue
        entry = TOOL_REGISTRY.get(name)
        if entry is None:
            raise ToolResolveError(
                f"Unknown tool '{name}'. Available: {sorted(TOOL_REGISTRY) + sorted(custom)}"
            )
        missing = [var for var in entry["requires"] if not os.getenv(var)]
        if missing:
            raise ToolResolveError(
                f"Tool '{name}' requires env vars that are not set: {', '.join(missing)}"
            )


def resolve_tools(tool_names: list[str], workspace_dir=None) -> list:
    """Instantiate fresh toolkit instances for a run.

    Raises ToolResolveError with a clear message for unknown names or missing
    credentials. Storage-backed toolkits (StorageToolkit / CMDToolkit /
    PythonInterpreterToolkit) get a LocalStorageHandler rooted at
    workspace_dir (the graph's workspace files/ dir) when provided. Custom
    tools (studio/data/tools/) build their own single-tool toolkit.
    """
    validate_tool_names(tool_names)
    import custom_tools

    custom = {t["name"]: t for t in custom_tools.list_custom_tools()}
    tools = []
    for name in tool_names or []:
        if name in custom:
            try:
                tools.append(custom_tools.make_toolkit(custom[name]))
            except Exception as e:
                raise ToolResolveError(f"Failed to build custom tool '{name}': {e}")
            continue
        entry = TOOL_REGISTRY[name]
        try:
            if entry.get("storage") and workspace_dir is not None:
                from evoagentx.tools.storage_handler import LocalStorageHandler

                handler = LocalStorageHandler(base_path=str(workspace_dir))
                tools.append(entry["factory"](storage_handler=handler))
            else:
                tools.append(entry["factory"]())
        except Exception as e:
            raise ToolResolveError(f"Failed to instantiate tool '{name}': {e}")
    return tools
