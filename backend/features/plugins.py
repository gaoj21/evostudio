"""Project plugins: task-specific code that Studio loads without knowing it.

Studio is a general workflow platform. Code that only makes sense for one
task — a domain dataset, its preset nodes, a lookup toolkit, a starter
template — lives in that task's project folder and reaches the platform
through a plugin module:

    projects/<project>/studio_plugin.py

Every attribute is optional:

    NAME = "My project"                 # shown in the feature catalog
    DESCRIPTION = "..."

    def presets() -> list[dict]          # palette node presets; each may set
                                         # "group" (palette section title)
    def templates() -> list[dict]        # {"id", "name", "description", "graph"}
    def toolkits() -> dict[str, dict]    # like tools_registry.TOOLKITS entries:
                                         # {"factory", "description", "requires",
                                         #  "tool_names"}
    def source_types() -> dict[str, dict]
        # Canvas Input types. Each is a source schema ({"label",
        # "description", "config", "outputs"}) plus hooks:
        #   "records": fn(config) -> list[dict]   (required) the records
        #   "info":    fn(params: dict) -> dict   details for forms (optional)
        #   "sequence": {"group": field, "order": field}
        #       records sharing `group` are one trajectory: run in `order`,
        #       and a failure blocks the rest of that trajectory (optional)
        #   "watch_key": field  — de-duplicates records when watched (optional)

The project folder's parent is put on sys.path, so a plugin imports its own
package by name (e.g. `from my_project.lib import x`). A plugin that fails to
import is reported, not fatal: the rest of Studio keeps working.

Set EAX_STUDIO_PROJECTS to a path list (os.pathsep-separated) to load plugins
from elsewhere; the default is the repository's projects/ folder.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_FILE = "studio_plugin.py"
SOURCE_HOOKS = ("records", "info", "sequence", "watch_key")

_lock = threading.Lock()
_loaded: list | None = None
_errors: dict[str, str] = {}


def roots() -> list[Path]:
    configured = os.environ.get("EAX_STUDIO_PROJECTS")
    if configured:
        return [Path(p).expanduser() for p in configured.split(os.pathsep) if p.strip()]
    return [_REPO_ROOT / "projects"]


def _load_one(path: Path):
    parent = str(path.parent.parent)
    if parent not in sys.path:
        sys.path.append(parent)
    name = f"studio_plugin_{path.parent.name}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    module.PROJECT = path.parent.name
    return module


def plugins() -> list:
    """Every loadable plugin module, loaded once per process."""
    global _loaded
    with _lock:
        if _loaded is None:
            _loaded, _errors_found = [], {}
            for root in roots():
                for path in sorted(root.glob(f"*/{PLUGIN_FILE}")):
                    try:
                        _loaded.append(_load_one(path))
                    except Exception as exc:  # a broken project must not break Studio
                        log.warning("Studio plugin %s failed to load: %s", path, exc)
                        _errors_found[path.parent.name] = f"{type(exc).__name__}: {exc}"
            _errors.clear()
            _errors.update(_errors_found)
        return list(_loaded)


def reload() -> None:
    """Forget loaded plugins (tests, or after installing a project)."""
    global _loaded
    with _lock:
        _loaded = None


def errors() -> dict[str, str]:
    plugins()
    return dict(_errors)


def _call(module, attr, default):
    fn = getattr(module, attr, None)
    if fn is None:
        return default
    try:
        return fn()
    except Exception as exc:
        log.warning("Studio plugin %s.%s() failed: %s", module.PROJECT, attr, exc)
        _errors[module.PROJECT] = f"{attr}(): {type(exc).__name__}: {exc}"
        return default


def presets() -> list[dict]:
    out = []
    for module in plugins():
        for preset in _call(module, "presets", []):
            out.append({"group": getattr(module, "NAME", module.PROJECT), **preset})
    return out


def templates() -> list[dict]:
    return [t for module in plugins() for t in _call(module, "templates", [])]


def toolkits() -> dict[str, dict]:
    out = {}
    for module in plugins():
        for name, entry in _call(module, "toolkits", {}).items():
            out[name] = {**entry, "project": module.PROJECT}
    return out


def _source_types() -> dict[str, dict]:
    out = {}
    for module in plugins():
        for type_, schema in _call(module, "source_types", {}).items():
            out[type_] = {**schema, "project": module.PROJECT}
    return out


def source_schemas() -> dict[str, dict]:
    """Plugin Input types as the canvas sees them: schema only, no hooks."""
    out = {}
    for type_, schema in _source_types().items():
        public = {k: v for k, v in schema.items() if k not in SOURCE_HOOKS}
        public["local"] = True
        if schema.get("sequence"):
            public["sequence"] = dict(schema["sequence"])
        if schema.get("info"):
            public["has_info"] = True
        out[type_] = public
    return out


def source_type(type_: str | None) -> dict | None:
    """The full plugin Input definition (with hooks), or None."""
    if not type_:
        return None
    return _source_types().get(type_)


def catalog() -> list[dict]:
    """What each project adds, for the feature catalog."""
    out = []
    for module in plugins():
        out.append({
            "name": module.PROJECT,
            "label": getattr(module, "NAME", module.PROJECT),
            "description": getattr(module, "DESCRIPTION", ""),
            "available": module.PROJECT not in _errors,
            "unavailable_reason": _errors.get(module.PROJECT),
        })
    for project, error in errors().items():
        if not any(p["name"] == project for p in out):
            out.append({"name": project, "label": project, "description": "",
                        "available": False, "unavailable_reason": error})
    return out
