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
import copy
import json
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from . import custom_tools
from . import graphs as graph_store
from . import runner
from . import skills_api
from . import tools_registry
from .graphs import is_source_task, is_tool_task, parked_task_names, topo_sort_tasks, migrate_flow
from .export_templates import _readme, _run_py, _workflow_py
from .project_import import (  # noqa: F401 - compatibility re-exports
    _graph_from_upload,
    _graph_from_workflow_py,
    _merge_code_into_graph,
    _place_new_nodes,
    import_graph,
    router as import_router,
)

router = APIRouter(prefix="/api")
router.include_router(import_router)

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

    Only what is actually reached: a project package can hold gigabytes of
    data, so bundling a whole package because one function imports one module of it is
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
                    # `from .sibling import X` inside a package
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

    A toolkit registered by Studio (e.g. one a project plugin adds) does not exist in
    a bare evoagentx install, so the export ships its source. Its own imports
    may still reach outside the bundle — the README says so rather than the
    project failing mysteriously.
    """
    builtins, local = [], {}
    for name in names:
        try:
            instance = tools_registry.toolkits()[name]["factory"]()
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

    # Exported as it would run: with the edges' mappings and the nodes'
    # enabled flags made explicit, the same way a stored graph is on load.
    graph = migrate_flow(copy.deepcopy(graph))
    tasks = graph.get("tasks", []) or []
    if any(t.get('kind') == 'evaluator' or (t.get('source') or {}).get('type') == 'dataloader' for t in tasks):
        raise HTTPException(422, 'DataLoader and Evaluator workflows run in Studio. Export the graph JSON to preserve their configuration; standalone Python export is not supported.')
    if any((t.get('harness') or {}).get('engine') == 'deepagents' for t in tasks):
        raise HTTPException(422, 'Standalone export of Deep Agents harness nodes is not supported yet. Run this workflow in the platform.')
    if any(t.get('use_long_term_memory') and (t.get('memory') or {}).get('provider') == 'mem0' for t in tasks):
        raise HTTPException(422, 'Standalone export of shared Mem0 spaces is not supported yet. Run this task in the platform, or use a separate copy without the Mem0 binding.')
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

    # A node names a *tool*; the thing to bundle is the *toolkit* exporting
    # it. Keyed by toolkit name this only worked when the two coincided —
    # a one-function module named after its function.
    custom = {tool["name"]: spec for spec in custom_tools.list_custom_tools()
              for tool in custom_tools.tools_of(spec)}
    bundled_custom = list({custom[t]["name"]: custom[t] for t in used_tools if t in custom}.values())
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
    from . import run_plan
    disabled = [t for t in ordered if t.get("name") in parked and not is_source_task(t)]
    files["workflow.py"] = _workflow_py(goal, llm_tasks, tool_nodes, edges,
                                        bundled_skills, graph_id, sorted(local_tools),
                                        plan_nodes=run_plan.compile_plan(graph)["nodes"],
                                        disabled=disabled)
    files["run.py"] = _run_py(name, required_inputs)
    files["requirements.txt"] = _requirements(needs_memory=uses_memory)
    tool_requirements = sorted({r for spec in bundled_custom
                                for r in (spec.get("requirements") or [])
                                + ((spec.get("package") or {}).get("requirements") or [])})
    if tool_requirements:
        files["requirements.txt"] += ("\n# What the bundled custom tools declared:\n"
                                      + "\n".join(tool_requirements) + "\n")
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
        local_tools, _local_tool_report(local_tools),
    )

    for module_name, source in local_tools.items():
        files[f"vendor/{module_name}.py"] = _relocate_root(source)

    if uses_memory:
        # The exported project must keep the same fields the canvas does, so
        # the rule lives in one module that travels with it rather than being
        # re-implemented in the generated file.
        files["vendor/memory_policy.py"] = (
            Path(__import__("backend.api.memory_policy", fromlist=["__file__"]).__file__).read_text(encoding="utf-8").replace("from . import bindings", "import memory_bindings as bindings")
        )
        files["vendor/memory_bindings.py"] = Path(__import__("backend.features.memory.bindings", fromlist=["__file__"]).__file__).read_text(encoding="utf-8")
        # A node that keeps a table needs the table, not a re-implementation.
        files["vendor/table_store.py"] = (
            Path(__import__("backend.api.table_store", fromlist=["__file__"]).__file__).read_text(encoding="utf-8").replace(
                "from backend.api.studio_config import data_path",
                "def data_path(*parts):\n    return Path(__file__).resolve().parent.joinpath(*parts)",
            ).replace("from .bindings import timestamp, BindingError", "from memory_bindings import timestamp, BindingError").replace("from .identity import storage_name, display_names", "def storage_name(graph_id, node): return node\ndef display_names(graph_id, names, *args): return sorted(names)")
        )

    binary_files: dict[str, bytes] = {}
    for spec in bundled_custom:
        if spec.get("package"):
            # An uploaded folder goes as a folder; its entry stays the API.
            folder = custom_tools.package_dir(spec["name"])
            for path in sorted(p for p in folder.rglob("*") if p.is_file()):
                rel = path.relative_to(folder).as_posix()
                if (any(part in SKIP_DIRS for part in path.relative_to(folder).parts)
                        or path.suffix in {".pyc", ".pyo"}):
                    continue
                try:
                    files[f"tools/{spec['name']}/{rel}"] = path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    binary_files[f"tools/{spec['name']}/{rel}"] = path.read_bytes()
        else:
            files[f"tools/{spec['name']}.py"] = spec["code"].rstrip() + "\n"
        # The whole stored spec but its code (which is the file beside it):
        # configuration, input-source marking and requirements included, so
        # the project runs the tool as Studio does and imports back whole.
        files[f"tools/{spec['name']}.json"] = json.dumps(
            {k: v for k, v in spec.items() if k != "code"},
            ensure_ascii=False, indent=2, default=str,
        )
    if bundled_custom:
        files["tools/__init__.py"] = ""
        # How Studio loads a toolkit and builds a class toolkit's instance.
        from backend.features.library import tool_runtime
        files["vendor/tool_runtime.py"] = Path(tool_runtime.__file__).read_text(encoding="utf-8")
    if any(spec.get("factory") for spec in bundled_custom):
        # A class-based tool runs through the same contract as in Studio:
        # its configuration applied to build_tool, its inputs checked.
        from backend.features.data import dataset_interface
        from backend.features.library import python_tool
        files["vendor/python_tool.py"] = Path(python_tool.__file__).read_text(encoding="utf-8").replace(
            "from backend.features.data.dataset_interface import", "from dataset_interface import")
        files["vendor/dataset_interface.py"] = Path(dataset_interface.__file__).read_text(encoding="utf-8")

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
    if not include_vendor:
        return files, binary_files
    for tree in VENDOR_TREES:
        root = _REPO_ROOT / "backend" / tree
        if not root.is_dir():
            continue
        for path in _iter_tree(root):
            binary_files[f"vendor/{path.relative_to(_REPO_ROOT / 'backend')}"] = path.read_bytes()

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
        root = _REPO_ROOT / "backend" / tree
        if not root.is_dir():
            continue
        for path in _iter_tree(root):
            stat = path.stat()
            digest.update(
                f"{path.relative_to(_REPO_ROOT)}:{stat.st_size}:{stat.st_mtime_ns}\n"
                .encode()
            )
    return digest.hexdigest()


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
