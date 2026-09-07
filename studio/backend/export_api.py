"""Export a canvas workflow as a standalone project.

`GET /api/graphs/{id}/export` returns a .zip holding everything needed to run
the workflow without Studio: the tasks as readable Python, a CLI runner, the
custom tools and skills it uses, a sample input taken from a real run, and a
README.

What is reproduced faithfully (it is the same execution path as runner.py):
tool nodes run first and deterministically, their structured results are
JSON-stringified for downstream LLM nodes, skills are appended to system
prompts, and the LLM tasks execute as a SequentialWorkFlowGraph.

What is not: source nodes (they pull from Studio-side feeds) become declared
inputs, and long-term memory is left off. Both are called out in the README so
the reader knows what changed rather than discovering it at run time.
"""

import io
import json
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import Response

from . import custom_tools
from . import graphs as graph_store
from . import runner
from . import skills_api
from . import tools_registry
from .graphs import (COL_WIDTH, ROW_HEIGHT, is_source_task, is_tool_task,
                    parked_task_names, topo_sort_tasks)

router = APIRouter(prefix="/api")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_DIR = Path(__file__).resolve().parent


def _manifest(graph: dict, tools, skills, local_tools, builtin_tools) -> dict:
    """Provenance for a deployed copy.

    Someone debugging a bundle three weeks from now needs to know which commit
    it came from and what went into it — including whether the tree had
    uncommitted changes, because the vendored framework is a copy of the
    working tree, not of a release.
    """
    import subprocess
    from datetime import datetime, timezone

    def git(*args) -> str:
        try:
            return subprocess.run(
                ["git", *args], cwd=_REPO_ROOT, capture_output=True,
                text=True, timeout=5,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    version = ""
    try:
        import re

        match = re.search(r'^version = "([^"]+)"',
                          (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"),
                          re.M)
        version = match.group(1) if match else ""
    except OSError:
        pass

    commit = git("rev-parse", "HEAD")
    return {
        "graph_id": graph.get("id"),
        "graph_name": graph.get("name"),
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "evoagentx_version": version,
        "source_commit": commit or None,
        # A dirty tree means the vendored framework does not match any commit.
        "source_dirty": bool(git("status", "--porcelain")) if commit else None,
        "bundled": {
            "vendor_trees": VENDOR_TREES,
            "custom_tools": sorted(t["name"] for t in tools),
            "skills": sorted(s["name"] for s in skills),
            "builtin_toolkits": sorted(builtin_tools),
            "local_toolkit_modules": sorted(local_tools),
        },
    }


def _requirements(needs_memory: bool = False) -> str:
    """The framework's own third-party dependencies.

    evoagentx itself is vendored rather than installed: this repo's copy
    carries local changes (skills, among others) that the published package
    does not have, so pinning the PyPI release would ship different behaviour
    than the canvas was built against.
    """
    import re

    deps = []
    try:
        text = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        block = re.search(r"^dependencies = \[(.*?)^\]", text, re.S | re.M)
        if block:
            deps = [line.strip().strip(",").strip('"')
                    for line in block.group(1).strip().splitlines() if line.strip()]
    except OSError:
        pass
    deps = [d for d in deps if d] or ["evoagentx>=0.1.4"]
    text = ("# evoagentx is vendored under vendor/ — these are its dependencies.\n"
            + "\n".join(deps) + "\n")
    if needs_memory:
        # Long-term memory runs on a local vector index and local embeddings.
        # Which ones depends on the backend, so both sets are spelled out.
        extras = _optional_deps("rag")
        if extras:
            text += ("\n# Long-term memory, EAX_MEMORY_BACKEND=framework (default):\n"
                     + "\n".join(extras) + "\n")
        text += ("\n# Long-term memory, EAX_MEMORY_BACKEND=langchain — uncomment\n"
                 "# instead of the block above if you switch backends:\n"
                 "# langchain-community\n"
                 "# langchain-huggingface\n"
                 "# faiss-cpu==1.8.0.post1\n"
                 "# sentence-transformers\n")
    return text


def _optional_deps(extra: str) -> list[str]:
    import re

    try:
        text = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return []
    block = re.search(rf"^{extra} = \[(.*?)^\]", text, re.S | re.M)
    if not block:
        return []
    return [line.strip().strip(",").strip('"')
            for line in block.group(1).strip().splitlines() if line.strip()]


def _py(value) -> str:
    """Pretty-print a JSON-ish value as a Python literal."""
    import pprint

    return pprint.pformat(value, width=88, sort_dicts=False)


def _default_model() -> tuple[str, str, str]:
    """The provider Studio is configured with: (model, base_url, key env var)."""
    try:
        from llm.registry import get_provider

        cfg = get_provider(None)
        return (cfg.get("model", ""), (cfg.get("base_url") or ""),
                cfg.get("api_key_env", ""))
    except Exception:
        return "", "", ""


# Vendored so the project deploys without this repo: `evoagentx` here carries
# local changes (the skills module among them) that a PyPI install does not.
VENDOR_TREES = ["evoagentx", "llm", "memory"]
SKIP_DIRS = {"__pycache__", ".versions", ".git", "node_modules", ".pytest_cache"}
MAX_VENDOR_FILE = 4 * 1024 * 1024
MAX_DATA_FILE = 8 * 1024 * 1024


def _iter_tree(root: Path):
    """Files under `root` worth shipping: no caches, no oversized blobs."""
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix in {".pyc", ".pyo", ".so", ".dylib"}:
            continue
        if path.stat().st_size > MAX_VENDOR_FILE:
            continue
        yield path


def _repo_modules_reached(sources: dict[str, str]) -> set[Path]:
    """Repo-local modules the given sources import, transitively.

    Only what is actually reached: `credit_risk` alone is 5.7 GB of dataset, so
    bundling a whole package because one function imports one module of it is
    not an option.
    """
    import ast

    reached: set[Path] = set()
    # (source, package it lives in) — the package is needed to resolve the
    # relative imports a package's own __init__.py is made of.
    queue = [(src, "") for src in sources.values()]
    seen_modules: set[str] = set()

    def repo_module_files(dotted: str) -> list[Path]:
        parts = dotted.split(".")
        base = _REPO_ROOT.joinpath(*parts)
        files = []
        if base.with_suffix(".py").is_file():
            files.append(base.with_suffix(".py"))
        elif base.is_dir() and (base / "__init__.py").is_file():
            files.append(base / "__init__.py")
        else:
            return []
        # every package __init__.py on the way down has to come too
        for i in range(1, len(parts)):
            init = _REPO_ROOT.joinpath(*parts[:i]) / "__init__.py"
            if init.is_file():
                files.append(init)
        return files

    def package_of(path: Path) -> str:
        rel = path.relative_to(_REPO_ROOT)
        parts = list(rel.parts[:-1]) if rel.name == "__init__.py" else list(rel.parts[:-1])
        return ".".join(parts)

    while queue:
        source, package = queue.pop()
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        dotted_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                dotted_names.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    dotted_names.add(node.module)
                elif node.level and package:
                    # `from .sourcing import X` inside credit_risk.agentic_pipeline
                    base = package.split(".")
                    base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
                    prefix = ".".join(base)
                    if node.module:
                        dotted_names.add(f"{prefix}.{node.module}" if prefix else node.module)
                    for alias in node.names:
                        # `from . import mod` names the module in `names`
                        dotted_names.add(f"{prefix}.{alias.name}" if prefix else alias.name)
        for dotted in dotted_names:
            if dotted in seen_modules or dotted.split(".")[0] in VENDOR_TREES:
                continue
            seen_modules.add(dotted)
            for path in repo_module_files(dotted):
                if path not in reached:
                    reached.add(path)
                    try:
                        queue.append((path.read_text(encoding="utf-8"), package_of(path)))
                    except OSError:
                        pass
    return reached


def _repo_data_files(sources: dict[str, str]) -> set[Path]:
    """Repo files a module builds a path to, e.g. ROOT / "a" / "b" / "c.csv".

    Matches the `/`-joined Path idiom these modules use; a literal that happens
    to name an existing repo file is taken too. Anything large is left out and
    reported instead of silently bloating the bundle.
    """
    import ast

    found: set[Path] = set()

    def segments(node):
        """Trailing string segments of a `x / "a" / "b"` chain, in order."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            left = segments(node.left)
            right = segments(node.right)
            if right is None:
                return None
            return (left or []) + right
        return None

    for source in sources.values():
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            parts = None
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                parts = segments(node)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                parts = [node.value] if "/" in node.value else None
            # Docstrings and prose look like nothing a filesystem accepts; a
            # path segment is short, single-line and non-empty.
            if not parts or any(
                not p or len(p) > 120 or "\n" in p or p in (".", "..")
                for p in parts
            ):
                continue
            try:
                candidate = _REPO_ROOT.joinpath(*parts)
                if candidate.is_file() and candidate.stat().st_size <= MAX_DATA_FILE:
                    found.add(candidate)
            except (OSError, ValueError):
                continue
    return found


def _relocate_root(source: str) -> str:
    """Re-point a relocated module's idea of the repository root.

    These modules find the repo with `Path(__file__).resolve().parents[N]`,
    counting the directories they sit in. Moving one into `vendor/` changes
    that depth, so the count is replaced by `parent` — and `vendor/` mirrors
    the repo layout, which is what those paths are relative to.
    """
    import re

    return re.sub(
        r"Path\(__file__\)\.resolve\(\)\.parents\[\d+\]",
        "Path(__file__).resolve().parent",
        source,
    )


def _classify_toolkits(names: list[str]) -> tuple[list[str], dict[str, str]]:
    """Split referenced toolkits into evoagentx built-ins and Studio-local ones.

    A toolkit registered by Studio (e.g. ObligorMatchToolkit) does not exist in
    a bare evoagentx install, so the export ships its source. Its own imports
    may still reach outside the bundle — the README says so rather than the
    project failing mysteriously.
    """
    builtins, local = [], {}
    for name in names:
        try:
            instance = tools_registry.TOOL_REGISTRY[name]["factory"]()
            module_name = type(instance).__module__
        except Exception:
            builtins.append(name)  # unavailable here; assume importable there
            continue
        if module_name.startswith("evoagentx."):
            builtins.append(name)
            continue
        try:
            import sys

            path = Path(sys.modules[module_name].__file__)
            local[module_name] = path.read_text(encoding="utf-8")
        except Exception:
            builtins.append(name)
    return builtins, local


def _local_tool_report(local: dict[str, str]) -> dict[str, list[str]]:
    bundled = set(local)
    return {name: _missing_deps(src, bundled) for name, src in local.items()}


def _missing_deps(source: str, bundled: set[str]) -> list[str]:
    """Top-level modules a bundled toolkit imports that the export cannot supply.

    Walks the whole AST, not just module-level imports: the toolkits that need
    outside code tend to import it lazily inside a function, which is exactly
    the case that fails halfway through a run rather than at start-up.
    """
    import ast
    import sys

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    known = set(sys.stdlib_module_names) | {"evoagentx"} | bundled
    return sorted(
        name for name in imported
        if name not in known and (_REPO_ROOT / name).exists()
    )


def _sample_from_runs(graph_id: str) -> dict | None:
    """Inputs of the most recent successful run, to ship as sample data."""
    for run in runner.list_runs(graph_id=graph_id):
        if run.get("status") == "success" and run.get("inputs"):
            full = runner.get_run(run["run_id"]) or run
            return {
                "run_id": run.get("run_id"),
                "inputs": full.get("inputs") or {},
                "result": full.get("result"),
            }
    return None


def build_project(graph: dict) -> tuple[str, bytes]:
    """Return (filename, zip bytes) for a canvas graph."""
    graph_id = graph.get("id") or "workflow"
    files, binary_files = project_files(graph)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in files.items():
            zf.writestr(f"{graph_id}/{path}", content)
        for path, blob in binary_files.items():
            zf.writestr(f"{graph_id}/{path}", blob)
    return f"{graph_id}.zip", buffer.getvalue()


def project_files(graph: dict, include_vendor: bool = True) -> tuple[dict, dict]:
    """Every file the exported project is made of, as (text, binary) maps.

    `include_vendor=False` leaves out the framework and the layers under it —
    262 files that are the same for every workflow and change only when this
    repo does. The workspace writer skips them unless they are stale, so
    saving a canvas does not re-read 3.8 MB each time.
    """
    graph_id = graph.get("id") or "workflow"
    name = graph.get("name") or graph_id
    goal = graph.get("goal") or ""

    tasks = graph.get("tasks", []) or []
    edges = graph.get("edges", []) or []
    ordered = topo_sort_tasks(tasks, edges)
    parked = parked_task_names(tasks, edges)
    active = [t for t in ordered if t.get("name") not in parked]

    source_nodes = [t for t in active if is_source_task(t)]
    tool_nodes = [t for t in active if is_tool_task(t)]
    llm_tasks = [t for t in active if not is_source_task(t) and not is_tool_task(t)]

    # Names the exported runner cannot produce itself: everything an LLM/tool
    # node consumes that no other exported node emits. Source-node outputs land
    # here too, which is exactly right — the caller must now supply them.
    produced = set()
    for task in tool_nodes + llm_tasks:
        for out in task.get("outputs") or []:
            if out.get("name"):
                produced.add(out["name"])
    required_inputs = []
    seen = set()
    for task in tool_nodes + llm_tasks:
        for inp in task.get("inputs") or []:
            n = inp.get("name")
            if n and n not in produced and n not in seen:
                seen.add(n)
                required_inputs.append(inp)

    used_tools = sorted({
        t for task in llm_tasks for t in (task.get("tool_names") or [])
    } | {t.get("tool") for t in tool_nodes if t.get("tool")})
    used_skills = sorted({
        s for task in llm_tasks for s in (task.get("skill_names") or [])
    })

    custom = {c["name"]: c for c in custom_tools.list_custom_tools()}
    bundled_custom = [custom[t] for t in used_tools if t in custom]
    builtin_tools, local_tools = _classify_toolkits(
        [t for t in used_tools if t not in custom]
    )

    all_skills = {s["name"]: s for s in skills_api.list_skills()}
    bundled_skills = [all_skills[s] for s in used_skills if s in all_skills]

    model, base_url, key_env = _default_model()
    sample = _sample_from_runs(graph_id)

    files: dict[str, str] = {}
    files["graph.json"] = json.dumps(graph, ensure_ascii=False, indent=2)
    files["manifest.json"] = json.dumps(
        _manifest(graph, bundled_custom, bundled_skills, local_tools, builtin_tools),
        ensure_ascii=False, indent=2,
    )
    uses_memory = any(t.get("use_long_term_memory") for t in llm_tasks)
    files["workflow.py"] = _workflow_py(goal, llm_tasks, tool_nodes, edges,
                                        bundled_skills, graph_id, sorted(local_tools))
    files["run.py"] = _run_py(name, required_inputs)
    files["requirements.txt"] = _requirements(needs_memory=uses_memory)
    files[".env.example"] = (
        "# The bundled provider layer (vendor/llm/providers.json) reads the key\n"
        "# for the provider it selects. This is the default path.\n"
        f"{key_env or 'DEEPSEEK_API_KEY'}=sk-...\n"
        "# EAX_PROVIDER=openai        # pick a non-default provider\n"
        "# EAX_MEMORY_BACKEND=langchain  # default: framework\n"
        "\n"
        "# Or bypass that layer entirely and talk to one endpoint:\n"
        f"# EAX_MODEL={model or 'deepseek/deepseek-chat'}\n"
        "# EAX_API_KEY=sk-...\n"
        + (f"# EAX_BASE_URL={base_url}\n" if base_url else "# EAX_BASE_URL=https://.../v1\n")
    )
    files["README.md"] = _readme(
        name, goal, llm_tasks, tool_nodes, source_nodes, edges,
        required_inputs, builtin_tools, bundled_custom, bundled_skills, sample, model,
        local_tools,
    )

    for module_name, source in local_tools.items():
        files[f"vendor/{module_name}.py"] = _relocate_root(source)

    if uses_memory:
        # The exported project must keep the same fields the canvas does, so
        # the rule lives in one module that travels with it rather than being
        # re-implemented in the generated file.
        files["vendor/memory_policy.py"] = (
            (_BACKEND_DIR / "memory_policy.py").read_text(encoding="utf-8")
        )
        # A node that keeps a table needs the table, not a re-implementation.
        files["vendor/table_store.py"] = (
            (_BACKEND_DIR / "table_store.py").read_text(encoding="utf-8")
        )

    for spec in bundled_custom:
        files[f"tools/{spec['name']}.py"] = spec["code"].rstrip() + "\n"
        files[f"tools/{spec['name']}.json"] = json.dumps(
            {k: spec[k] for k in ("name", "description", "tools")},
            ensure_ascii=False, indent=2,
        )
    if bundled_custom:
        files["tools/__init__.py"] = ""

    for skill in bundled_skills:
        files[f"skills/{skill['name']}/SKILL.md"] = (
            "---\n"
            f"name: {skill['name']}\n"
            f"description: {skill['description']}\n"
            "---\n\n" + skill["content"].strip() + "\n"
        )

    if sample:
        files["data/sample_input.json"] = json.dumps(sample["inputs"], ensure_ascii=False, indent=2)
        if sample.get("result") is not None:
            files["data/sample_output.json"] = json.dumps(sample["result"], ensure_ascii=False, indent=2)
    else:
        files["data/sample_input.json"] = json.dumps(
            {i["name"]: "" for i in required_inputs}, ensure_ascii=False, indent=2
        )

    # Everything the project needs to run on another machine: the framework as
    # it exists here (local changes included), the provider and memory layers,
    # and whatever repo modules the bundled toolkits actually import.
    binary_files: dict[str, bytes] = {}
    if not include_vendor:
        return files, binary_files
    for tree in VENDOR_TREES:
        root = _REPO_ROOT / tree
        if not root.is_dir():
            continue
        for path in _iter_tree(root):
            binary_files[f"vendor/{path.relative_to(_REPO_ROOT)}"] = path.read_bytes()

    reached = _repo_modules_reached(local_tools)
    for path in sorted(reached):
        binary_files[f"vendor/{path.relative_to(_REPO_ROOT)}"] = path.read_bytes()
    data_sources = dict(local_tools)
    data_sources.update({str(p): p.read_text(encoding="utf-8", errors="ignore")
                         for p in reached})
    for path in sorted(_repo_data_files(data_sources)):
        binary_files[f"vendor/{path.relative_to(_REPO_ROOT)}"] = path.read_bytes()

    return files, binary_files


def vendor_fingerprint() -> str:
    """What the vendored trees look like right now.

    Path, size and mtime of everything that would be bundled — enough to tell
    that nothing has changed, without reading 3.8 MB to find out.
    """
    import hashlib

    digest = hashlib.sha1()
    for tree in VENDOR_TREES:
        root = _REPO_ROOT / tree
        if not root.is_dir():
            continue
        for path in _iter_tree(root):
            stat = path.stat()
            digest.update(
                f"{path.relative_to(_REPO_ROOT)}:{stat.st_size}:{stat.st_mtime_ns}\n"
                .encode()
            )
    return digest.hexdigest()


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
            local_tools=None) -> str:
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
        report = _local_tool_report(local_tools)
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


@router.get("/graphs/{graph_id}/export")
def export_graph(graph_id: str):
    graph = graph_store.load_graph(graph_id)
    if graph is None:
        raise HTTPException(status_code=404, detail=f"Graph '{graph_id}' not found")
    try:
        filename, payload = build_project(graph)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Export failed: {type(e).__name__}: {e}")
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Import: the other direction
# ---------------------------------------------------------------------------

def _graph_from_upload(filename: str, payload: bytes) -> tuple[dict, dict, dict, list[str]]:
    """Read a graph out of an uploaded file.

    Accepts a project zip produced by the export above, a bare graph.json, or a
    bare workflow.py. Returns (graph, custom_tools, skills, notes).

    Inside a project, `workflow.py` is the source of truth: it is what someone
    edits after the export, so its tasks and wiring win over graph.json, which
    only keeps what the code cannot express (canvas positions, source nodes).
    """
    name = (filename or "").lower()
    if name.endswith(".py"):
        code = _graph_from_workflow_py(payload.decode("utf-8", errors="replace"))
        if code is None:
            raise HTTPException(
                status_code=422,
                detail="Could not read TASKS/EDGES out of that workflow.py "
                       "(they must stay plain literals).",
            )
        graph = {"name": "Imported workflow", **code}
        graph_store.auto_layout(graph)
        return graph, {}, {}, ["built the canvas from workflow.py"]
    if name.endswith(".json") or payload[:1] == b"{":
        try:
            return json.loads(payload.decode("utf-8")), {}, {}, []
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise HTTPException(status_code=422, detail=f"Not valid JSON: {e}")

    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile:
        raise HTTPException(
            status_code=422,
            detail="Upload a graph.json or a project .zip exported from Studio.",
        )

    graph_entry = next(
        (n for n in archive.namelist() if n.rsplit("/", 1)[-1] == "graph.json"), None
    )
    if graph_entry is None:
        raise HTTPException(status_code=422, detail="No graph.json inside the archive")
    try:
        graph = json.loads(archive.read(graph_entry).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=422, detail=f"graph.json is not valid JSON: {e}")

    root = graph_entry[: -len("graph.json")]
    tools: dict[str, dict] = {}
    skills: dict[str, dict] = {}
    for entry in archive.namelist():
        if not entry.startswith(root):
            continue
        rel = entry[len(root):]
        if rel.startswith("tools/") and rel.endswith(".json"):
            try:
                spec = json.loads(archive.read(entry).decode("utf-8"))
                code_entry = entry[: -len(".json")] + ".py"
                spec["code"] = archive.read(code_entry).decode("utf-8")
                tools[spec["name"]] = spec
            except (KeyError, ValueError, UnicodeDecodeError):
                continue  # a half-written tool must not sink the whole import
        elif rel.startswith("skills/") and rel.endswith("/SKILL.md"):
            try:
                text = archive.read(entry).decode("utf-8")
            except UnicodeDecodeError:
                continue
            frontmatter, _, body = text.partition("---\n")[2].partition("\n---")
            meta = {}
            for line in frontmatter.splitlines():
                key, _, value = line.partition(":")
                if key.strip() in ("name", "description"):
                    meta[key.strip()] = value.strip()
            skill_name = meta.get("name") or rel.split("/")[1]
            skills[skill_name] = {
                "name": skill_name,
                "description": meta.get("description", ""),
                "content": body.strip(),
            }

    notes: list[str] = []
    workflow_entry = f"{root}workflow.py"
    if workflow_entry in archive.namelist():
        try:
            code = _graph_from_workflow_py(
                archive.read(workflow_entry).decode("utf-8", errors="replace")
            )
        except KeyError:
            code = None
        if code is None:
            notes.append(
                "workflow.py could not be read back (TASKS/EDGES are no longer "
                "plain literals) — imported graph.json instead"
            )
        else:
            graph, merge_notes = _merge_code_into_graph(graph, code)
            notes.append("workflow.py is the source of truth for this import")
            notes.extend(merge_notes)
    return graph, tools, skills, notes


@router.post("/graphs/import")
async def import_graph(file: UploadFile):
    """Create a workflow from an exported project or a graph.json.

    Always creates a new workflow rather than overwriting one: an import is
    usually someone else's copy coming back, and silently replacing the graph
    open on the canvas would be the wrong default.

    Bundled tools and skills are installed only when a name is free — an
    existing one is left alone and reported, so an import cannot quietly
    rewrite the code a different workflow depends on.

    When the archive holds a `workflow.py`, that file decides the tasks and
    wiring: it is what someone edits after taking the project away.
    """
    payload = await file.read()
    graph, tools, skills, notes = _graph_from_upload(file.filename, payload)
    if not isinstance(graph, dict) or not isinstance(graph.get("tasks"), list):
        raise HTTPException(status_code=422, detail="That file is not a workflow graph")

    installed_tools, installed_skills = [], []
    existing_tools = {t["name"] for t in custom_tools.list_custom_tools()}
    for name, spec in tools.items():
        if name in existing_tools:
            notes.append(f"Tool '{name}' already exists — kept the existing one")
            continue
        try:
            custom_tools.save_custom_tool(
                custom_tools.validate_spec(spec, tools_registry.builtin_names())
            )
            installed_tools.append(name)
        except Exception as e:
            notes.append(f"Tool '{name}' was not installed: {e}")

    existing_skills = {s["name"] for s in skills_api.list_skills()}
    for name, spec in skills.items():
        if name in existing_skills:
            notes.append(f"Skill '{name}' already exists — kept the existing one")
            continue
        try:
            skills_api.save_skill(spec)
            installed_skills.append(name)
        except Exception as e:
            notes.append(f"Skill '{name}' was not installed: {e}")

    created = graph_store.create_graph(
        graph.get("name") or "Imported workflow", graph.get("goal") or ""
    )
    body = {
        "id": created["id"],
        "name": created["name"],
        "goal": graph.get("goal") or "",
        "output_dir": graph.get("output_dir") or "runs",
        "tasks": graph.get("tasks") or [],
        "edges": graph.get("edges") or [],
    }
    try:
        saved = graph_store.save_graph(created["id"], body)
    except graph_store.GraphValidationError as e:
        graph_store.delete_graph(created["id"])
        raise HTTPException(status_code=422, detail=[str(x) for x in e.errors])

    return {
        **saved,
        "imported_tools": installed_tools,
        "imported_skills": installed_skills,
        "notes": notes,
    }


def _graph_from_workflow_py(source: str) -> dict | None:
    """Read TASKS / TOOL_NODES / EDGES / GOAL back out of an exported workflow.py.

    Only literal assignments are read — the file is parsed, never executed, so
    importing someone's project cannot run their code. A file edited into
    something non-literal (a comprehension, a variable) simply does not parse
    back, and the caller falls back to graph.json.
    """
    import ast

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    found: dict[str, object] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or target.id not in (
            "GOAL", "TASKS", "TOOL_NODES", "EDGES"
        ):
            continue
        try:
            found[target.id] = ast.literal_eval(node.value)
        except (ValueError, SyntaxError):
            return None

    if not isinstance(found.get("TASKS"), list) or not isinstance(found.get("EDGES"), list):
        return None

    tasks = [dict(t) for t in found["TASKS"] if isinstance(t, dict)]
    for tool in found.get("TOOL_NODES") or []:
        if isinstance(tool, dict):
            tasks.append({**tool, "kind": "tool"})
    return {
        "goal": found.get("GOAL") or "",
        "tasks": tasks,
        "edges": [e for e in found["EDGES"] if isinstance(e, dict)],
    }


def _merge_code_into_graph(graph: dict, code: dict) -> tuple[dict, list[str]]:
    """Let the code win, without losing what the code cannot express.

    `workflow.py` carries the tasks, their wiring and their prompts. It does
    not carry canvas positions or source nodes (whose feeds became inputs on
    export), so those are taken from graph.json and kept.
    """
    by_name = {t.get("name"): t for t in graph.get("tasks") or []}
    source_nodes = [t for t in graph.get("tasks") or [] if is_source_task(t)]
    notes: list[str] = []

    merged: list[dict] = list(source_nodes)
    code_names = set()
    for task in code["tasks"]:
        name = task.get("name")
        if not name:
            continue
        code_names.add(name)
        previous = by_name.get(name) or {}
        # position is canvas-only; everything else comes from the code
        merged.append({
            **({"x": previous["x"]} if "x" in previous else {}),
            **({"y": previous["y"]} if "y" in previous else {}),
            **({"save_output": previous["save_output"]} if "save_output" in previous else {}),
            **task,
        })
        if not previous:
            notes.append(f"'{name}' added from workflow.py")
            continue
        # Compare only what the code actually carries: the export drops falsy
        # fields, so a full dict comparison would call every node changed.
        changed = sorted(
            key for key, value in task.items()
            if key not in ("x", "y") and previous.get(key) != value
        )
        if changed:
            notes.append(f"'{name}' updated from workflow.py ({', '.join(changed)})")

    dropped = [n for n in by_name
               if n not in code_names and not is_source_task(by_name[n])]
    for name in dropped:
        notes.append(f"'{name}' is not in workflow.py — removed")

    result = {**graph, "goal": code.get("goal") or graph.get("goal") or "",
              "tasks": merged, "edges": code["edges"]}
    placed = _place_new_nodes(result)
    if placed:
        notes.append(f"placed {', '.join(placed)} on the canvas")
    return result, notes


def _place_new_nodes(graph: dict) -> list[str]:
    """Give coordinates to nodes the code introduced, and only to those.

    Re-running the full layout would throw away an arrangement the user made
    by hand, so a new node is put one column right of whatever feeds it, at a
    row that is still free.
    """
    tasks = graph.get("tasks") or []
    placed_by = {e.get("target"): e.get("source") for e in graph.get("edges") or []}
    positioned = {t["name"]: t for t in tasks if "x" in t and "y" in t}
    taken = {(t["x"], t["y"]) for t in positioned.values()}
    placed = []
    for task in tasks:
        if "x" in task and "y" in task:
            continue
        upstream = positioned.get(placed_by.get(task.get("name")) or "")
        x = (upstream["x"] + COL_WIDTH) if upstream else 0
        y = upstream["y"] if upstream else 0
        while (x, y) in taken:
            y += ROW_HEIGHT
        task["x"], task["y"] = x, y
        taken.add((x, y))
        positioned[task["name"]] = task
        placed.append(f"'{task['name']}'")
    return placed
