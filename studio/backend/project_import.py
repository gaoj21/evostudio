"""Import exported Studio projects back into canvas graphs."""

import io
import json
import zipfile

from fastapi import APIRouter, HTTPException, UploadFile

from . import custom_tools
from . import graphs as graph_store
from . import skills_api
from . import tools_registry
from .graphs import COL_WIDTH, ROW_HEIGHT, is_source_task

router = APIRouter(prefix="/api")

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
