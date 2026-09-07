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

Stored as JSON at studio/data/tools/<toolkit>.json:
  {name, description, code, tools: [{name, description, params}]}
A tool's return value (anything JSON-serializable) becomes its result.

Execution happens in a SUBPROCESS (the runner's worker threads make
signal-based timeouts unreliable): a wrapper under .venv/bin/python reads
{"code", "args"} from stdin, execs the user code, calls run(**args) and
prints the JSON result on stdout. 30s timeout; non-zero exit / timeout /
exception all surface as {"error": ...}.

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

from fastapi import APIRouter, Body, HTTPException

_REPO_ROOT = Path(__file__).resolve().parents[2]

TOOLS_DIR = _REPO_ROOT / "studio" / "data" / "tools"

ALLOWED_TYPES = ["string", "number", "integer", "boolean", "object", "array"]
_PY_TYPES = {"string": str, "integer": int, "number": float,
             "boolean": bool, "object": dict, "array": list}

EXEC_TIMEOUT = 30

_lock = threading.Lock()

_WRAPPER = r"""
import json, sys

payload = json.loads(sys.stdin.read())
ns = {}
try:
    exec(compile(payload["code"], "<custom_tool>", "exec"), ns)
    entry = ns.get(payload["entry"])
    if not callable(entry):
        raise ValueError(f"code does not define a callable {payload['entry']!r}")
    result = entry(**payload["args"])
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, default=str))
except Exception as e:
    print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
    sys.exit(1)
"""


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
    """A function's summary and its per-parameter descriptions.

    Reads the Google style the standard library and most tooling use: the text
    up to an `Args:` line describes the function, and the indented lines under
    it describe one parameter each.
    """
    import ast
    import textwrap

    raw = ast.get_docstring(func) or ""
    summary_lines: list[str] = []
    params: dict[str, str] = {}
    in_args = False
    current = None
    for line in textwrap.dedent(raw).splitlines():
        stripped = line.strip()
        if stripped.lower() in ("args:", "arguments:", "parameters:"):
            in_args = True
            continue
        if in_args:
            if stripped and not line.startswith((" ", "\t")) and stripped.endswith(":"):
                in_args = False        # a following section: Returns:, Raises:
                continue
            if not stripped:
                continue
            name, sep, text = stripped.partition(":")
            if sep and name.strip().isidentifier():
                current = name.strip()
                params[current] = text.strip()
            elif current:
                params[current] = f"{params[current]} {stripped}".strip()
        else:
            summary_lines.append(stripped)
    return " ".join(l for l in summary_lines if l).strip(), params


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


def _one_tool(func) -> dict:
    """One public function, as the tool it describes."""
    import ast

    if isinstance(func, ast.AsyncFunctionDef):
        raise CustomToolError(
            f"'{func.name}' is async. A tool is called directly in a "
            "subprocess, so it has to be an ordinary function."
        )

    description, param_docs = _docstring_parts(func)
    if not description:
        raise CustomToolError(
            f"'{func.name}' needs a docstring: it is what the model reads to "
            "decide when to call it. A tool without a description is not a "
            "tool — prefix the name with an underscore if it is a helper."
        )

    args = func.args
    if args.vararg or args.kwarg:
        raise CustomToolError(
            f"'{func.name}' takes *args/**kwargs — a tool's parameters have to "
            "be named, so the model knows what it can pass."
        )

    params = []
    for arg in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
        if arg.arg == "self":
            raise CustomToolError("'self' is not a valid parameter name")
        annotation = _annotation_name(arg.annotation) if arg.annotation else None
        if annotation is None:
            raise CustomToolError(
                f"Parameter '{arg.arg}' of '{func.name}' has no type "
                f"annotation. Annotate it (one of: "
                f"{', '.join(sorted(_TYPE_FROM_ANNOTATION))}) — the model uses "
                "the type to decide what to pass."
            )
        json_type = _TYPE_FROM_ANNOTATION.get(annotation.lower())
        if json_type is None:
            raise CustomToolError(
                f"Parameter '{arg.arg}' of '{func.name}' is annotated "
                f"{annotation!r}, which has no equivalent the model can be "
                f"told about. Use one of: "
                f"{', '.join(sorted(_TYPE_FROM_ANNOTATION))}."
            )
        params.append({"name": arg.arg, "type": json_type,
                       "description": param_docs.get(arg.arg, "")})

    return {"name": func.name, "description": description, "params": params}


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
            "interface: define at least one public function — wrapping a "
            "library or an existing project is exactly the point — and give "
            "it a docstring."
        )
    tools = [_one_tool(f) for f in functions]
    seen = set()
    for tool in tools:
        if tool["name"] in seen:
            raise CustomToolError(f"Two functions are both named {tool['name']!r}")
        seen.add(tool["name"])
    return {"description": _module_description(code), "tools": tools}


def validate_spec(spec: dict, builtin_names: list[str],
                  existing: list[dict] | None = None) -> dict:
    """The toolkit a spec's code defines, ready to store.

    Everything but the code and the toolkit's own name is derived. The name
    defaults to the single tool's, so the common case — one function — needs
    nothing but the code.
    """
    code = spec.get("code") or ""
    derived = analyse(code)
    tools = derived["tools"]

    name = (spec.get("name") or "").strip()
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

    return {"name": name, "description": description, "tools": tools, "code": code}


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


def save_custom_tool(spec: dict) -> dict:
    with _lock:
        TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        with open(_path(spec["name"]), "w", encoding="utf-8") as f:
            json.dump(spec, f, indent=2, ensure_ascii=False)
    return spec


def delete_custom_tool(name: str) -> bool:
    with _lock:
        path = _path(name)
        if not path.is_file():
            return False
        path.unlink()
        return True


# ---------------------------------------------------------------------------
# Execution (subprocess) + dynamic framework Tool
# ---------------------------------------------------------------------------

def run_custom_tool(name: str, args: dict) -> dict:
    """Call one custom tool in a subprocess; always returns a dict,
    {"error": ...} on any failure.

    `name` is the tool, not the toolkit: a module exports several, and a node
    refers to the one it wants.
    """
    found = find(name)
    if found is None:
        return {"error": f"Custom tool '{name}' not found"}
    spec, _tool = found
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _WRAPPER],
            input=json.dumps({"code": spec["code"], "args": args,
                              "entry": name},
                             ensure_ascii=False, default=str),
            capture_output=True, text=True, timeout=EXEC_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"Custom tool '{name}' timed out after {EXEC_TIMEOUT}s"}
    out = (proc.stdout or "").strip()
    try:
        payload = json.loads(out.splitlines()[-1]) if out else {}
    except json.JSONDecodeError:
        return {"error": f"Custom tool '{name}' produced no JSON result: "
                         f"{(proc.stderr or out)[:300]}"}
    if proc.returncode != 0 or not payload.get("ok"):
        return {"error": payload.get("error") or (proc.stderr or "")[:300]
                or f"exit code {proc.returncode}"}
    return {"result": payload.get("result")}


def make_toolkit(spec: dict):
    """Build a framework Toolkit holding every tool the module exports.

    Each Tool subclass gets a __call__ whose parameter names and annotations
    exactly match its own params — the framework's Tool metaclass validates
    that at class creation.
    """
    from evoagentx.tools.tool import Tool, Toolkit

    return Toolkit(name=spec["name"],
                   tools=[_make_tool(t)() for t in tools_of(spec)])


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
    sig = ", ".join(f"{p['name']}: {_PY_TYPES[p['type']].__name__}" for p in params)
    kwargs = ", ".join(f"'{p['name']}': {p['name']}" for p in params)
    src = (
        f"def __call__(self{', ' if sig else ''}{sig}):\n"
        f"    return run_custom_tool(self.name, {{{kwargs}}})\n"
    )
    ns = {"run_custom_tool": run_custom_tool}
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
        "required": [p["name"] for p in params],
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
    import tools_registry

    try:
        spec = validate_spec(body, tools_registry.builtin_names())
    except CustomToolError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return save_custom_tool(spec)


@router.delete("/tools/custom/{name}")
def delete_custom(name: str):
    if not delete_custom_tool(name):
        raise HTTPException(status_code=404, detail=f"Custom tool '{name}' not found")
    return {"ok": True}
