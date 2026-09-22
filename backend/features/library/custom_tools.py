"""User-defined tools for EvoAgentX Studio.

A tool is a documented calling interface. Not necessarily a function of its
own: a library or an existing project is a tool too, as long as it exposes an
API and says what the API does. So what is stored here is a *toolkit* — a
Python module — and every public callable it exposes is one tool:

    'Quotes and price history from Yahoo Finance.'     <- the toolkit
                                                          (a module docstring)
    import yfinance

    def quote(symbol: str) -> dict:                    <- one tool
        'The latest traded price for a ticker.

         Args:
             symbol: ticker, e.g. AAPL'
        return {"price": yfinance.Ticker(symbol).fast_info["lastPrice"]}

    def history(symbol: str, days: int) -> list:       <- another
        'Daily closing prices over the last N days. ...'

    def _session():                                    <- a helper, not a tool
        ...

(the quotes above are written singly only so this example can live inside a
docstring; real toolkits use ordinary triple-quoted docstrings)

Wrapping something that already exists is the point: the module can import an
installed library, or put a project on sys.path and re-export its API. What
makes it a tool is the description, not who wrote the code underneath.

Everything is derived from the module (`analyse`) rather than declared beside
it, so the two cannot disagree: the module docstring says what the toolkit is,
each public function's name names a tool, its docstring is what the model
reads to decide when to call it, its signature is the parameter list, and its
annotations are the parameter types. A leading underscore marks a helper.

A toolkit can also be one class -- the only public one, the one TOOL_CLASS
names, or whatever build_tool(...) returns. Then every public method is a
tool and the constructor's typed parameters are the toolkit's configuration,
set once when it is saved and shown as a form, exactly as a DataLoader's
build_dataset parameters are. Within one run a class toolkit keeps one
instance, so a tool can set something up and the next call finds it there.
`interface` says which of the three a module is, reading the code only.

Stored as JSON at studio-data/tools/<toolkit>.json:
  {name, description, code, tools: [{name, description, params}]}
  class toolkits also: {kind: "class", target, configuration, config}
A tool's return value (anything JSON-serializable) becomes its result.

Execution happens in a SUBPROCESS (the runner's worker threads make
signal-based timeouts unreliable): tool_worker.py, started by
tool_sessions.py, loads the module and answers one JSON request per line.
One process per call, or one per run for a class toolkit. 30s per request;
a timeout, a crash or an exception all surface as {"error": ...}, and an
exception says which line of the author's code raised it and what it printed.

Trust model: Studio is a local single-user tool — custom code is the user's
own code running with the user's own permissions, same as editing any script
in this repo. There is no sandboxing by design.

Routes: GET/POST/DELETE /api/tools/custom (mounted via this module's router).
"""

import json
import subprocess
import sys
import threading
from pathlib import Path
from backend.api.studio_config import data_path

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile

_REPO_ROOT = Path(__file__).resolve().parents[3]

TOOLS_DIR = data_path("tools")

ALLOWED_TYPES = ["string", "number", "integer", "boolean", "object", "array"]
_PY_TYPES = {"string": str, "integer": int, "number": float,
             "boolean": bool, "object": dict, "array": list}

EXEC_TIMEOUT = 30

_lock = threading.Lock()

class CustomToolError(Exception):
    """User-facing custom-tool validation error (HTTP 422)."""


def _validate_name(name: str, builtins: list[str]) -> str:
    import re

    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]*", name or ""):
        raise CustomToolError(
            f"Invalid tool name {name!r}: must be an identifier "
            "(letter, then letters/digits/underscores)"
        )
    if name in builtins:
        raise CustomToolError(f"Tool name {name!r} conflicts with a built-in tool")
    return name


# Annotations the model can be told about. `str` and friends are spelled the
# way Python spells them; the stored spec uses JSON-schema names.
_TYPE_FROM_ANNOTATION = {
    "str": "string", "int": "integer", "float": "number", "bool": "boolean",
    "dict": "object", "list": "array",
}


def _annotation_name(node) -> str | None:
    """The plain name of an annotation, or None if it is not one we know.

    Only the simple forms are read: `str`, `list`, `dict[str, int]`. Anything
    more elaborate has no JSON-schema equivalent to give the model.
    """
    import ast

    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Subscript):        # list[str] -> list
        return _annotation_name(node.value)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value                      # a string annotation
    if isinstance(node, ast.Attribute):        # typing.Dict -> Dict
        return node.attr
    return None


def _docstring_parts(func) -> tuple[str, dict[str, str]]:
    """A function's summary and its per-parameter descriptions (Google style:
    the text up to an `Args:` line, then one indented line per parameter)."""
    import ast

    from .tool_runtime import parse_docstring
    return parse_docstring(ast.get_docstring(func) or "")


def _public_functions(code: str) -> list:
    """The module's public top-level functions, in source order.

    A leading underscore marks a helper, the way it does everywhere else in
    Python, so a toolkit is free to have them.
    """
    import ast

    try:
        module = ast.parse(code)
    except SyntaxError as e:
        raise CustomToolError(f"Code does not compile: {e}")
    return [n for n in module.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not n.name.startswith("_")]


def _module_description(code: str) -> str:
    """What the toolkit as a whole is, from the module docstring."""
    import ast

    return (ast.get_docstring(ast.parse(code)) or "").strip().split("\n\n")[0] \
        .replace("\n", " ").strip()


def _one_tool(func, method: bool = False) -> dict:
    """One public function (or a class's public method), as the tool it
    describes. A method's `self` / `cls` is not a parameter the model passes."""
    import ast

    from .tool_runtime import tool_schema

    args = func.args
    positional = list(args.posonlyargs) + list(args.args)
    # A parameter with a default is optional: the model may leave it out, and
    # a source node's config may leave it blank.
    defaulted = {a.arg for a in positional[len(positional) - len(args.defaults):]} if args.defaults else set()
    defaulted |= {a.arg for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None}
    if method and not any(ast.unparse(d) == "staticmethod" for d in func.decorator_list):
        positional = positional[1:]
    params = [(arg.arg, _annotation_name(arg.annotation) if arg.annotation else None, arg.arg in defaulted)
              for arg in positional + list(args.kwonlyargs)]
    try:
        return tool_schema(func.name, ast.get_docstring(func) or "", params,
                           is_async=isinstance(func, ast.AsyncFunctionDef),
                           star=bool(args.vararg or args.kwarg))
    except ValueError as e:
        raise CustomToolError(str(e)) from e


# ---------------------------------------------------------------------------
# A class as a toolkit
# ---------------------------------------------------------------------------

_NOT_METHODS = ("property", "cached_property", ".setter", ".getter", ".deleter")


def _class_methods(cls, classes: dict, seen: tuple = ()) -> dict:
    """A class's methods by name, its bases' first when this module defines
    them too. Properties are attributes, not calls."""
    import ast

    methods = {}
    for base in cls.bases:
        if isinstance(base, ast.Name) and base.id in classes and base.id not in seen:
            methods.update(_class_methods(classes[base.id], classes, seen + (cls.name,)))
    for node in cls.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(ast.unparse(d).endswith(_NOT_METHODS) for d in node.decorator_list):
            methods.pop(node.name, None)
            continue
        methods[node.name] = node
    return methods


def _public_methods(cls, classes: dict) -> list:
    return [m for name, m in _class_methods(cls, classes).items() if not name.startswith("_")]


def _returned_class(factory) -> str | None:
    """The class build_tool says it returns: its annotation, or the one class
    every `return Name(...)` in it builds."""
    import ast

    if factory.returns is not None:
        name = _annotation_name(factory.returns)
        if name:
            return name
    built = {ast.unparse(n.value.func) for n in ast.walk(factory)
             if isinstance(n, ast.Return) and isinstance(n.value, ast.Call)}
    return next(iter(built)) if len(built) == 1 else None


def _class_target(module) -> dict | None:
    """Which class a module's tools are the methods of, as far as reading the
    code can tell. None when the module is not a class toolkit.

    `build_tool(...)` or `TOOL_CLASS = SomeClass` says so explicitly;
    otherwise a module with no public functions and one public class with
    public methods is that class's toolkit. `class` is None when the class
    is not defined in this module (imported from a library or a sibling
    module): it is then read from the live module instead.
    """
    import ast

    classes = {n.name: n for n in module.body if isinstance(n, ast.ClassDef)}
    factory = next((n for n in module.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == "build_tool"), None)
    if factory is not None:
        return {"target": {"factory": "build_tool"}, "class": classes.get(_returned_class(factory) or "")}
    marker = [n.value for n in module.body if isinstance(n, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id == "TOOL_CLASS" for t in n.targets)]
    if marker:
        named = marker[-1].id if isinstance(marker[-1], ast.Name) else None
        if named is None:
            raise CustomToolError("TOOL_CLASS must name a class, e.g. TOOL_CLASS = MyTool.")
        return {"target": {"class": "TOOL_CLASS"}, "class": classes.get(named)}
    if [n for n in module.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not n.name.startswith("_")]:
        return None
    candidates = [c for c in classes.values()
                  if not c.name.startswith("_") and _public_methods(c, classes)]
    if len(candidates) > 1:
        raise CustomToolError(
            f"Several public classes have public methods ({', '.join(c.name for c in candidates)}). "
            "Say which one is the toolkit with TOOL_CLASS = <name> (or a build_tool "
            "factory), or prefix the others with an underscore.")
    if candidates:
        return {"target": {"class": candidates[0].name}, "class": candidates[0]}
    return None


def _configuration(code: str, module, found: dict) -> dict:
    """The toolkit's configuration form: build_tool's typed parameters, or
    the class constructor's -- read the way a DataLoader's build_dataset is."""
    import ast
    import copy

    from backend.features.data.dataset_interface import describe
    try:
        if found["target"].get("factory"):
            info = describe(code, "build_tool", {"config"})
        else:
            cls = found["class"]
            classes = {n.name: n for n in module.body if isinstance(n, ast.ClassDef)}
            init = _class_methods(cls, classes).get("__init__") if cls is not None else None
            if init is None:
                return {"entrypoint": f"{cls.name}()" if cls is not None else "TOOL_CLASS()",
                        "inputs": [], "warnings": []}
            fn = copy.deepcopy(init)
            fn.name, fn.decorator_list = "__init__", []
            positional = fn.args.posonlyargs or fn.args.args
            if positional:
                positional.pop(0)          # self
            info = describe(ast.unparse(ast.Module(body=[fn], type_ignores=[])), "__init__", {"config"})
            info["entrypoint"] = f"{cls.name}.__init__"
    except ValueError as e:
        raise CustomToolError(str(e)) from e
    return {k: info[k] for k in ("entrypoint", "inputs", "warnings") if k in info}


def interface(code: str) -> dict:
    """Everything reading a toolkit's code can say about it, without running it.

    {kind, description, tools, configuration, target, needs_discovery}:
    kind is "function" (public functions), "class" (a class's public
    methods, built once from its configuration) or "factory" (the older
    build_tool + INPUT_SCHEMA contract). needs_discovery: the class is
    imported, so its methods can only be read from the live module.
    """
    import ast

    from . import python_tool
    try:
        module = ast.parse(code)
    except SyntaxError as e:
        raise CustomToolError(f"Code does not compile: line {e.lineno}: {e.msg}")
    if _legacy_factory(module):
        try:
            schema = python_tool.interface(code)
        except (ValueError, TypeError) as e:
            raise CustomToolError(str(e)) from e
        return {"kind": "factory", "factory": True, **schema, "description": _module_description(code)}
    found = _class_target(module)
    if found is None:
        return {"kind": "function", "factory": False, "configuration": None, **analyse(code)}
    classes = {n.name: n for n in module.body if isinstance(n, ast.ClassDef)}
    cls = found["class"]
    tools = [_one_tool(m, method=True) for m in _public_methods(cls, classes)] if cls is not None else []
    if cls is not None and not tools:
        raise CustomToolError(f"{cls.name} has no public methods: each public method is one tool.")
    _unique(tools)
    description = _module_description(code) or (
        (ast.get_docstring(cls) or "").strip().split("\n\n")[0].replace("\n", " ").strip() if cls is not None else "")
    # Bases this module does not define: in an uploaded folder they may be
    # the author's own (a sibling module), with methods that are tools too.
    external = sorted({ast.unparse(b) for b in (cls.bases if cls is not None else [])
                       if not (isinstance(b, ast.Name) and (b.id in classes or b.id == "object"))})
    return {"kind": "class", "factory": False, "description": description, "tools": tools,
            "configuration": _configuration(code, module, found), "target": found["target"],
            "class": cls.name if cls is not None else None, "needs_discovery": cls is None,
            "external_bases": external}


def _legacy_factory(module) -> bool:
    """The older class contract: build_tool plus a literal INPUT_SCHEMA."""
    import ast

    names = {t.id for n in module.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
    names |= {n.target.id for n in module.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)}
    return "INPUT_SCHEMA" in names and any(
        isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == "build_tool"
        for n in module.body)


def _unique(tools: list) -> None:
    seen = set()
    for tool in tools:
        if tool["name"] in seen:
            raise CustomToolError(f"Two functions are both named {tool['name']!r}")
        seen.add(tool["name"])


def analyse(code: str) -> dict:
    """The toolkit a module defines: {description, tools: [...]}.

    Raises CustomToolError with something actionable when the code does not
    describe one — an unannotated argument is refused rather than guessed at,
    because the model uses the type to decide what to pass.
    """
    functions = _public_functions(code)
    if not functions:
        raise CustomToolError(
            "Code exposes nothing to call. A tool is a documented calling "
            "interface: define at least one public function, or a class whose "
            "public methods are the tools (TOOL_CLASS = <an imported class> "
            "works too) — wrapping a library or an existing project is exactly "
            "the point — and give each a docstring."
        )
    tools = [_one_tool(f) for f in functions]
    _unique(tools)
    return {"description": _module_description(code), "tools": tools}


def validate_spec(spec: dict, builtin_names: list[str],
                  existing: list[dict] | None = None, folder: Path | None = None) -> dict:
    """The toolkit a spec's code defines, ready to store.

    Everything but the code, the toolkit's own name and its configuration
    values is derived. The name defaults to the single tool's (or the
    class's), so the common case needs nothing but the code.

    A class the code imports (TOOL_CLASS = SomeImportedClass) can only be
    read from the live module: the module is then loaded, in an isolated
    worker, to read it. `folder` is where an uploaded toolkit's files are
    when they are not yet in place (an upload being checked).
    """
    code = spec.get("code") or ""
    info = interface(code)
    kind = info["kind"]
    config = None
    if kind == "factory":
        from . import python_tool
        name = _validate_name((spec.get('name') or '').strip(), builtin_names)
        description = _module_description(code) or (spec.get('description') or '').strip()
        if not description: raise CustomToolError('Add a module docstring describing when to use this tool.')
        derived = {'description':description, 'tools':[{'name':name,'description':description,
            'params':[{'name':f['name'],'type':python_tool.JSON_TYPES[f['type']],'required':f['required'],'description':''} for f in info['inputs']],
            'outputs':info['outputs'], 'factory':True}]}
        from backend.features.data.dataset_interface import arguments
        try: config = arguments(code, spec.get('config') or {}, 'build_tool', set())
        except (ValueError,TypeError) as exc: raise CustomToolError(str(exc)) from exc
    elif kind == "class":
        package = spec.get("package")
        if package is None and folder is None:
            # Re-saving an uploaded toolkit (a source mark, a setting): its
            # class is read from its own folder.
            package = _saved_package((spec.get("name") or "").strip())
        if info["needs_discovery"] or (info["external_bases"] and (package or folder)):
            info["needs_discovery"] = True
            found = discover({**spec, "code": code, "target": info["target"], "package": package,
                              "configuration": info["configuration"]["inputs"]}, folder)
            if info["target"].get("class"):
                # An imported class: its constructor is the configuration.
                info["configuration"] = {"entrypoint": f"{found['class']}.__init__",
                                         "inputs": found["configuration"], "warnings": []}
            info.update(tools=found["tools"], **{"class": found["class"]})
            info["description"] = info["description"] or found.get("class_description") or ""
        from .tool_runtime import apply_config
        try:
            config = spec.get("config") or {}
            apply_config(info["configuration"]["inputs"], config)
        except ValueError as exc:
            raise CustomToolError(str(exc)) from exc
        derived = {"description": info["description"], "tools": info["tools"]}
    else:
        derived = {"description": info["description"], "tools": info["tools"]}
    factory = kind == "factory"
    tools = derived["tools"]

    name = (spec.get("name") or "").strip()
    if not name and kind == "class" and info.get("class"):
        name = info["class"]
    if not name:
        if len(tools) > 1:
            raise CustomToolError(
                "This module exposes "
                f"{len(tools)} tools ({', '.join(t['name'] for t in tools)}), "
                "so it needs a name of its own to group them under."
            )
        name = tools[0]["name"]
    _validate_name(name, builtin_names)

    description = derived["description"] or (spec.get("description") or "").strip()
    if not description:
        if len(tools) > 1:
            raise CustomToolError(
                "Give the module a docstring saying what this toolkit is; it "
                "is what the model sees before any individual tool."
            )
        description = tools[0]["description"]

    # A task refers to a tool by name alone, so two toolkits cannot both
    # export one: the reference would be ambiguous and silently pick one.
    taken = {t["name"]: spec_name
             for spec_name, names in _exported_names(existing, skip=name).items()
             for t in [{"name": n} for n in names]}
    clashes = sorted({t["name"] for t in tools} & set(taken))
    if clashes:
        raise CustomToolError(
            f"{clashes} already exported by toolkit '{taken[clashes[0]]}'. "
            "Tool names have to be unique — a node refers to one by name alone."
        )
    for tool in tools:
        if tool["name"] in builtin_names:
            raise CustomToolError(
                f"Tool name {tool['name']!r} conflicts with a built-in tool"
            )

    # Which of the functions may feed a workflow as an input source. Any
    # tool returning a record (a dict) or records (a list of dicts) can; the
    # author says which ones mean to, so a helper never turns up in the
    # source palette by accident.
    wanted = [str(n) for n in (spec.get("sources") or [])]
    exported = {t["name"] for t in tools}
    unknown = [n for n in wanted if n not in exported]
    if unknown:
        raise CustomToolError(
            f"{unknown} cannot be input sources: not exported by this module "
            f"(it exports {sorted(exported)})."
        )
    result = {"name": name, "description": description, "tools": tools, "code": code,
              "sources": wanted, "requirements": spec.get("requirements") or [], "tests": spec.get("tests") or []}
    from backend.features.library.tool_verification import configuration
    if factory: result.update(factory=True, config=config, interface={k: info[k] for k in ("configuration", "inputs", "outputs")})
    if kind == "class":
        # One instance per run, built from this configuration.
        result.update(kind="class", config=config, target=info["target"], tool_class=info.get("class"),
                      configuration=info["configuration"]["inputs"],
                      discovery="runtime" if info["needs_discovery"] else "static")
    configuration(result)
    return result


_PARAM_FIELD_TYPES = {"integer": "number", "number": "number", "boolean": "select"}


def custom_source_types() -> dict:
    """Source types contributed by custom toolkits, in the schema shape the
    canvas already understands: `custom:<tool>` with the tool's parameters
    as its config fields. Outputs are not known until the tool has run, so
    the node declares them (or probes for them)."""
    out = {}
    for spec in list_custom_tools():
        for tool in tools_of(spec):
            if tool["name"] not in (spec.get("sources") or []):
                continue
            fields = []
            for param in tool.get("params") or []:
                kind = _PARAM_FIELD_TYPES.get(param.get("type"), "text")
                field = {"name": param["name"], "label": param["name"], "type": kind,
                         "default": "" if kind != "number" else 0,
                         "required": param.get("required", True)}
                if kind == "select":
                    field["options"] = ["true", "false"]
                fields.append(field)
            out[f"custom:{tool['name']}"] = {
                "label": tool["name"],
                "description": tool.get("description") or spec.get("description") or "",
                "config": fields,
                "outputs": [],
                "custom": True,
                "tool": tool["name"],
                "toolkit": spec["name"],
            }
    return out


def _exported_names(existing: list[dict] | None, skip: str) -> dict[str, list[str]]:
    """Tool names each stored toolkit exports, excluding the one being saved."""
    out = {}
    for spec in (existing if existing is not None else list_custom_tools()):
        if spec.get("name") == skip:
            continue
        out[spec["name"]] = [t["name"] for t in tools_of(spec)]
    return out


def tools_of(spec: dict) -> list[dict]:
    """The tools a stored toolkit exports.

    Tolerates the older shape, where a stored tool was a single function and
    its parameters sat at the top level.
    """
    if spec.get("tools"):
        return spec["tools"]
    return [{"name": spec.get("name"), "description": spec.get("description", ""),
             "params": spec.get("params") or []}]


def find(tool_name: str):
    """The (toolkit, tool) a name refers to, or None."""
    for spec in list_custom_tools():
        for tool in tools_of(spec):
            if tool["name"] == tool_name:
                return spec, tool
    return None


def iter_tools():
    """Every custom tool there is, as (toolkit, tool) pairs."""
    for spec in list_custom_tools():
        for tool in tools_of(spec):
            yield spec, tool


def _path(name: str) -> Path:
    return TOOLS_DIR / f"{name}.json"


def list_custom_tools() -> list[dict]:
    if not TOOLS_DIR.is_dir():
        return []
    out = []
    for path in sorted(TOOLS_DIR.glob("*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                out.append(json.load(f))
        except (json.JSONDecodeError, OSError):
            continue
    return out


def package_dir(name: str) -> Path:
    """Where an uploaded toolkit's files live: a folder beside its spec."""
    return TOOLS_DIR / name


def save_custom_tool(spec: dict) -> dict:
    with _lock:
        TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        # Re-saving a packaged toolkit (marking a source, say) must not drop
        # the package or swap its code for a pasted copy.
        previous = _read_spec(spec["name"])
        if previous and previous.get("package") and not spec.get("package"):
            if (spec.get("code") or "") != previous.get("code"):
                # The folder's entry file is the code that runs, so what is
                # advertised is re-derived from it too; a list of tools read
                # off the pasted copy would name functions nothing defines.
                from backend.api import tools_registry
                spec = validate_spec({**spec, "code": previous["code"]},
                                     tools_registry.builtin_names())
            spec = {**spec, "package": previous["package"], "code": previous["code"]}
        with open(_path(spec["name"]), "w", encoding="utf-8") as f:
            json.dump(spec, f, indent=2, ensure_ascii=False)
    return spec


def _saved_package(name: str) -> dict | None:
    """The package of a stored toolkit, if it is an uploaded one."""
    import re

    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]*", name or ""):
        return None
    return (_read_spec(name) or {}).get("package")


def _read_spec(name: str) -> dict | None:
    try:
        with open(_path(name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def delete_custom_tool(name: str) -> bool:
    import shutil

    with _lock:
        path = _path(name)
        if not path.is_file():
            return False
        path.unlink()
        folder = package_dir(name)
        if folder.is_dir():
            shutil.rmtree(folder)
        return True


# ---------------------------------------------------------------------------
# Uploaded toolkits: a folder, a library, a project
# ---------------------------------------------------------------------------

MAX_PACKAGE_BYTES = 50 * 1024 * 1024
_ENTRY_CANDIDATES = ("tools.py", "main.py", "api.py", "__init__.py")


def _members(archive) -> list:
    """The archive's files, refused if any would land outside the folder."""
    out = []
    for info in archive.infolist():
        name = info.filename
        if name.endswith("/"):
            continue
        parts = Path(name).parts
        if not parts or name.startswith("/") or ".." in parts or ":" in parts[0]:
            raise CustomToolError(f"Archive member {name!r} would escape the toolkit folder.")
        out.append(info)
    return out


def _strip_root(names: list[str]) -> str:
    """The single top-level folder a zip-of-a-folder usually has, or ''."""
    roots = {Path(n).parts[0] for n in names}
    if len(roots) == 1:
        root = next(iter(roots))
        # A folder that is itself a package (`pkg/__init__.py`) is the
        # toolkit, not a wrapper around it: its relative imports need it kept.
        if all(len(Path(n).parts) > 1 for n in names) and f"{root}/__init__.py" not in names:
            return root
    return ""


def _pick_entry(files: list[str], wanted: str | None) -> str:
    """Which file is the toolkit's API.

    Named, or found: `tools.py` / `main.py` / `api.py` / a package's
    `__init__.py` at the top level, or the only .py file there. Anything
    else is ambiguous and is asked for rather than guessed.
    """
    if wanted:
        if wanted not in files:
            raise CustomToolError(f"Entry {wanted!r} is not in the archive (it has "
                                  f"{sorted(f for f in files if f.endswith('.py'))[:12]}).")
        return wanted
    top = [f for f in files if "/" not in f and f.endswith(".py")]
    for candidate in _ENTRY_CANDIDATES:
        if candidate in top:
            return candidate
    packages = sorted({f.split("/")[0] for f in files
                       if f.count("/") == 1 and f.endswith("/__init__.py")})
    if len(packages) == 1 and not top:
        return f"{packages[0]}/__init__.py"
    if len(top) == 1:
        return top[0]
    raise CustomToolError(
        "Which file is the toolkit's API? Name the entry file — none of "
        f"{list(_ENTRY_CANDIDATES)} is at the top level and there are "
        f"{len(top)} .py files there.")


def unpack_toolkit(blob: bytes, name: str | None, entry: str | None,
                   sources: list[str], builtin_names: list[str], check: bool = False) -> dict:
    """Store an uploaded folder (a zip) as a toolkit.

    The entry module's public functions (or its class's public methods) are
    the tools, exactly as for pasted code — the same analysis runs on it — and
    the rest of the folder travels with it: sibling modules, data files, a
    requirements.txt (listed, not installed: that is a separate, deliberate
    step). With check, nothing is stored: the result says what saving would.
    """
    import io
    import shutil
    import tempfile
    import zipfile

    if len(blob) > MAX_PACKAGE_BYTES:
        raise CustomToolError(f"Archive is {len(blob) // 1048576} MB; the limit is "
                              f"{MAX_PACKAGE_BYTES // 1048576} MB.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        raise CustomToolError("Not a zip archive. Zip the folder and upload that.")
    members = _members(archive)
    root = _strip_root([m.filename for m in members])
    rel = {m.filename: (m.filename[len(root) + 1:] if root else m.filename) for m in members}
    files = sorted(v for v in rel.values() if v)
    entry_file = _pick_entry(files, entry)
    code = archive.read(next(k for k, v in rel.items() if v == entry_file)).decode("utf-8", "replace")

    requirements = []
    req = next((k for k, v in rel.items() if v == "requirements.txt"), None)
    if req:
        requirements = [line.strip() for line in archive.read(req).decode("utf-8", "replace").splitlines()
                        if line.strip() and not line.strip().startswith("#")]

    # Named after the folder when not named: `my-project` becomes `my_project`,
    # since a toolkit name has to be something a task can refer to.
    import re
    from_folder = re.sub(r"[^0-9A-Za-z_]", "_", root).strip("_") if root else ""
    if from_folder and from_folder[0].isdigit():
        from_folder = "_" + from_folder
    package = {"entry": entry_file, "files": len(files), "requirements": requirements}
    # Unpacked aside first: a class the entry imports from a sibling module
    # is read from the live folder, before anything is replaced.
    staging = Path(tempfile.mkdtemp(prefix="studio-toolkit-"))
    try:
        for m in members:
            target = staging / rel[m.filename]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(m.filename))
        spec = validate_spec({"code": code, "name": name or from_folder or None, "sources": sources,
                              "package": package}, builtin_names, folder=staging)
        spec["package"] = package
        if check:
            return {**spec, "checked": True}
        folder = package_dir(spec["name"])
        with _lock:
            if folder.is_dir():
                shutil.rmtree(folder)
            folder.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(staging, folder)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    save_custom_tool(spec)
    return spec


def zip_files(files: list[tuple[str, bytes]]) -> bytes:
    """Loose files -- a folder picked in the browser, or a few .py files --
    as the zip an upload is, each at its relative path."""
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, blob in files:
            archive.writestr(path.replace("\\", "/").lstrip("/"), blob)
    return buffer.getvalue()


def save_package_entry(name: str, body: dict, builtin_names: list[str]) -> dict:
    """Save an edited entry file of an uploaded toolkit back into its folder.

    The folder's entry file is the code that runs, so an edit made in Studio
    is written there -- the spec's copy and the file never disagree.
    """
    previous = _read_spec(name)
    if not previous or not previous.get("package"):
        raise CustomToolError(f"'{name}' is not an uploaded toolkit.")
    package = previous["package"]
    folder = package_dir(name).resolve()
    entry = (folder / (package.get("entry") or "tools.py")).resolve()
    if folder not in entry.parents:
        raise CustomToolError("The entry file is outside the toolkit folder.")
    spec = validate_spec({**body, "name": name, "package": package}, builtin_names)
    with _lock:
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text(spec["code"], encoding="utf-8")
    return save_custom_tool({**spec, "package": package})


def install_requirements(name: str) -> dict:
    """Install a packaged toolkit's requirements into Studio's own Python.

    Deliberate and separate from upload: installing whatever a zip asks for
    on arrival is not something to do silently.
    """
    spec = _read_spec(name)
    if not spec or not spec.get("package"):
        raise CustomToolError(f"'{name}' is not an uploaded toolkit.")
    req = package_dir(name) / "requirements.txt"
    if not req.is_file():
        return {"installed": [], "output": "No requirements.txt in this toolkit."}
    proc = subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(req)],
                          capture_output=True, text=True, timeout=600)
    tail = (proc.stdout + proc.stderr)[-2000:]
    if proc.returncode != 0:
        raise CustomToolError(f"pip failed:\n{tail}")
    return {"installed": spec["package"].get("requirements", []), "output": tail}


# ---------------------------------------------------------------------------
# Execution (subprocess) + dynamic framework Tool
# ---------------------------------------------------------------------------

def run_custom_tool(name: str, args: dict, scope=None) -> dict:
    """Call one custom tool in an isolated worker; always returns a dict,
    {"error": ...} on any failure.

    `name` is the tool, not the toolkit: a module exports several, and a node
    refers to the one it wants. Inside a run scope (tool_sessions.scope), a
    class toolkit's instance is kept from one call to the next.
    """
    found = find(name)
    if found is None:
        return {"error": f"Custom tool '{name}' not found"}
    spec, _tool = found
    return run_spec(spec, name, args, scope=scope)


def load_payload(spec: dict, folder: Path | None = None) -> dict:
    """What the worker needs to load a toolkit (tool_worker.py)."""
    from backend.features.library.tool_verification import dependency_path
    package = spec.get("package")
    if folder is None and package:
        folder = package_dir(spec["name"])
    kind = spec.get("kind") or ("factory" if spec.get("factory") else "function")
    return {"code": spec.get("code") or "", "name": spec.get("name") or "toolkit", "kind": kind,
            "config": spec.get("config") or {}, "target": spec.get("target"),
            "configuration": spec.get("configuration") or [],
            "dependency_dir": str(dependency_path(spec)) if spec.get("requirements") else None,
            "package_dir": str(folder) if folder is not None else None,
            "entry_file": (package or {}).get("entry") if folder is not None else None,
            # The oldest stored shape: a single function called run.
            "legacy_run": not spec.get("tools")}


def run_spec(spec, name, args, scope=None, logs=False, folder=None):
    """Call tool `name` of a toolkit spec (saved or a draft)."""
    from . import tool_sessions
    load = load_payload(spec, folder)
    request = {"op": "call", "tool": name, "args": args}
    active = scope or tool_sessions.current()
    try:
        if load["kind"] == "class" and active is not None:
            reply = active.request(load, request, EXEC_TIMEOUT)
        else:
            reply = tool_sessions.once(load, request, EXEC_TIMEOUT)
    except tool_sessions.ToolTimeout:
        return {"error": f"Custom tool '{name}' timed out after {EXEC_TIMEOUT}s"}
    except tool_sessions.ToolError as e:
        return {"error": str(e), **({"logs": e.logs} if logs else {})}
    return {"result": reply.get("result"), **({"logs": reply.get("logs") or ""} if logs else {})}


def discover(spec: dict, folder: Path | None = None) -> dict:
    """A class toolkit's tools and configuration, read off the live module
    in an isolated worker: {class, configuration, tools}."""
    from . import tool_sessions
    load = {**load_payload(spec, folder), "kind": "discover"}
    try:
        return tool_sessions.once(load, {"op": "discover"}, EXEC_TIMEOUT)["result"]
    except tool_sessions.ToolTimeout:
        raise CustomToolError(f"Reading the toolkit's class timed out after {EXEC_TIMEOUT}s.")
    except tool_sessions.ToolError as e:
        raise CustomToolError(str(e)) from e


def make_toolkit(spec: dict):
    """Build a framework Toolkit holding every tool the module exports.

    Each Tool subclass gets a __call__ whose parameter names and annotations
    exactly match its own params — the framework's Tool metaclass validates
    that at class creation.
    """
    from evoagentx.tools.tool import Toolkit

    from . import tool_sessions
    # The run building this toolkit, kept on each tool: the framework may
    # call a tool from a thread the run's context does not reach, and a
    # class toolkit's instance belongs to the run.
    scope = tool_sessions.current()
    tools = []
    for t in tools_of(spec):
        tool = _make_tool(t)()
        object.__setattr__(tool, "_studio_scope", scope)
        tools.append(tool)
    return Toolkit(name=spec["name"], tools=tools)


# Generated Tool classes, kept by the tool they came from. The framework
# registers every subclass in a process-wide registry keyed on the class name
# and rejects a second one, so building a toolkit twice — which is what a
# second run of the same workflow does — used to raise "Found duplicate
# module". Identical tools reuse their class; an edited one gets a fresh name.
_TOOL_CLASSES: dict[str, type] = {}


def _make_tool(tool: dict):
    import hashlib

    from evoagentx.tools.tool import Tool

    key = json.dumps(tool, sort_keys=True, ensure_ascii=False, default=str)
    cached = _TOOL_CLASSES.get(key)
    if cached is not None:
        return cached

    params = tool.get("params") or []
    ordered = sorted(params, key=lambda p: not p.get('required', True))
    sig = ", ".join(f"{p['name']}: {_PY_TYPES[p['type']].__name__}" + (" = _MISSING" if not p.get('required', True) else "") for p in ordered)
    kwargs = ", ".join(f"'{p['name']}': {p['name']}" for p in params)
    src = (
        f"def __call__(self{', ' if sig else ''}{sig}):\n"
        f"    return run_custom_tool(self.name, {{k:v for k,v in {{{kwargs}}}.items() if v is not _MISSING}}, getattr(self, '_studio_scope', None))\n"
    )
    ns = {"run_custom_tool": run_custom_tool, "_MISSING":object()}
    exec(compile(src, "<custom_tool_class>", "exec"), ns)
    # The class name only has to be unique in the registry; the tool's own
    # `name` is what the framework and the model use.
    cls_name = f"{tool['name']}_{hashlib.sha1(key.encode()).hexdigest()[:8]}"
    cls = type(cls_name, (Tool,), {
        "__annotations__": {"name": str, "description": str,
                            "inputs": dict, "required": list},
        "name": tool["name"],
        "description": tool["description"],
        "inputs": {p["name"]: {"type": p["type"],
                               "description": p.get("description", "")}
                   for p in params},
        "required": [p["name"] for p in params if p.get("required", True)],
        "__call__": ns["__call__"],
    })
    _TOOL_CLASSES[key] = cls
    return cls


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api")


@router.get("/tools/custom")
def list_custom():
    return {"tools": list_custom_tools()}


@router.post("/tools/custom")
def save_custom(body: dict = Body(...)):
    from backend.api import tools_registry
    previous = _read_spec((body.get("name") or "").strip()) if isinstance(body, dict) else None
    if (previous and previous.get("package") and "code" in body
            and (body.get("code") or "") != previous.get("code")):
        if body.get("edit_entry") is not True:
            raise HTTPException(status_code=422, detail=(
                f"'{previous['name']}' is an uploaded folder toolkit: its code is the "
                f"files in that folder ({previous['package'].get('entry') or 'tools.py'} "
                "is the entry). Save with edit_entry to write this code into that "
                "entry file, or edit the folder and upload it again."))
        try:
            return save_package_entry(previous["name"], body, tools_registry.builtin_names())
        except CustomToolError as e:
            raise HTTPException(status_code=422, detail=str(e))
    try:
        spec = validate_spec(body, tools_registry.builtin_names())
        return save_custom_tool(spec)
    except CustomToolError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/tools/custom/upload")
async def upload_custom(file: UploadFile | None = File(default=None),
                        files: list[UploadFile] = File(default=[]),
                        name: str = Form(default=""), entry: str = Form(default=""),
                        sources: str = Form(default="[]"), check: bool = Form(default=False)):
    """A toolkit as a folder: a zip of a library or a project, or its files
    (a folder picked in the browser, a few .py files) at their relative
    paths. With check, what it would expose is reported and nothing is kept."""
    from backend.api import tools_registry
    try:
        marked = json.loads(sources or "[]")
    except json.JSONDecodeError:
        raise HTTPException(status_code=422, detail="sources must be a JSON list of names")
    uploads = ([file] if file is not None else []) + list(files or [])
    if not uploads:
        raise HTTPException(status_code=422, detail="Choose a .zip, a folder or .py files.")
    try:
        if len(uploads) == 1 and (uploads[0].filename or "").lower().endswith(".zip"):
            blob = await uploads[0].read()
        else:
            loose = [(u.filename or "", await u.read()) for u in uploads]
            if sum(len(b) for _, b in loose) > MAX_PACKAGE_BYTES:
                raise CustomToolError(f"The files add up to more than {MAX_PACKAGE_BYTES // 1048576} MB.")
            if not any(path.endswith(".py") for path, _ in loose):
                raise CustomToolError("None of these files is Python (.py); upload a .zip or the folder's .py files.")
            blob = zip_files(loose)
        return unpack_toolkit(blob, name.strip() or None, entry.strip() or None,
                              marked, tools_registry.builtin_names(), check=check)
    except CustomToolError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/tools/custom/{name}/install")
def install_custom(name: str):
    try:
        return install_requirements(name)
    except CustomToolError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=422, detail="pip took longer than 10 minutes; giving up.")


@router.delete("/tools/custom/{name}")
def delete_custom(name: str):
    if not delete_custom_tool(name):
        raise HTTPException(status_code=404, detail=f"Custom tool '{name}' not found")
    return {"ok": True}


@router.post("/tools/custom/{name}/verify")
def verify_custom(name: str):
    from backend.features.library.tool_verification import verify_saved
    try:
        return verify_saved(name)
    except CustomToolError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post('/tools/custom/interface')
def inspect_tool(body: dict = Body(...)):
    """What a toolkit's code exposes -- its kind, tools and their schemas,
    its configuration form -- read without running it. A class the code
    imports is read from the live module only when asked (execute: true);
    `name` finds an uploaded toolkit's folder for that."""
    try:
        code = body.get('code') or ''
        info = interface(code)
        if info.get('needs_discovery') and body.get('execute') is True:
            name = (body.get('name') or '').strip()
            found = discover({'code': code, 'name': name or None, 'target': info['target'],
                              'configuration': info['configuration']['inputs'],
                              'config': body.get('config') or {}, 'package': _saved_package(name)})
            if info['target'].get('class'):
                info['configuration'] = {'entrypoint': f"{found['class']}.__init__",
                                         'inputs': found['configuration'], 'warnings': []}
            info.update(tools=found['tools'], needs_discovery=False, discovered=True,
                        description=info['description'] or found.get('class_description') or '',
                        **{'class': found['class']})
        return info
    except (SyntaxError, ValueError, TypeError, CustomToolError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post('/tools/custom/preview')
def preview_tool(body: dict = Body(...)):
    """Call a draft toolkit without saving it. `calls` (a list of {tool,
    args}) makes several calls on one instance, as one run would."""
    from backend.api import tools_registry

    from . import tool_sessions
    try:
        spec = validate_spec(body, tools_registry.builtin_names())
        package = _saved_package(spec['name'])
        if package:
            # A draft of an uploaded toolkit's entry runs inside its folder.
            spec['package'] = package
        calls = body.get('calls') or [{'tool': body.get('tool'), 'args': body.get('args', {})}]
        if not isinstance(calls, list) or len(calls) > 20:
            raise CustomToolError('calls must be a list of at most 20 {tool, args}.')
        exported = {t['name'] for t in spec['tools']}
        results = []
        with tool_sessions.scope() as scope:
            for call in calls:
                name = (call or {}).get('tool') or spec['tools'][0]['name']
                if name not in exported:
                    raise CustomToolError('Choose an exported tool.')
                args = (call or {}).get('args', {})
                if not isinstance(args, dict): raise CustomToolError('Test inputs must be an object.')
                result = run_spec(spec, name, args or {}, scope=scope, logs=True)
                if 'error' in result: raise CustomToolError(result['error'])
                results.append({'tool': name, **result})
        last = results[-1]
        return {'status':'success', 'result': last['result'], 'logs': last.get('logs', ''),
                **({'results': results} if len(results) > 1 else {}),
                'scope':'This example only; not a guarantee for all inputs.'}
    except CustomToolError as exc:
        raise HTTPException(422, str(exc)) from exc
