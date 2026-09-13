"""EvoAgentX Studio backend — FastAPI app per studio/API.md (v1, MVP).

Run from the repository root:  uvicorn backend.api.app:app --port 8000
or:                            python -m backend.api.app
"""

import json
from contextlib import asynccontextmanager
from pathlib import Path

from datetime import datetime, timezone
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from . import agent_api
from . import batch as batch_store
from . import batch_compare
from . import batch_export
from . import chat_api, result_chat
from . import custom_tools
from . import evaluation
from . import export_api
from . import features
from . import graphs as graph_store
from . import memory_api
from . import projects
from . import preprocess
from . import registry
from . import review as review_store
from . import run_plan
from . import runner
from . import scheduler
from . import skills_api
from . import sources, source_collection
from . import tools_registry
from . import watcher
from . import workspace
from . import workspace_api

try:
    from . import evolve_api
except ModuleNotFoundError as exc:
    if exc.name not in {"dspy", "optuna"}:
        raise
    evolve_api = None
    _EVOLVE_IMPORT_ERROR = exc.name
else:
    _EVOLVE_IMPORT_ERROR = None

_REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIST = _REPO_ROOT / "frontend" / "dist"

PALETTE = {
    "templates": [
        {
            "type": "task",
            "label": "LLM Task",
            "description": "A general-purpose LLM task with custom inputs and outputs.",
            "defaults": {
                "description": "Describe what this task does.",
                "inputs": [
                    {"name": "input", "type": "str", "description": "Input text", "required": True}
                ],
                "outputs": [
                    {"name": "output", "type": "str", "description": "Output text", "required": True}
                ],
                "prompt": "Process the input:\n{input}",
                "parse_mode": "str",
            },
        },
        {
            "type": "summarizer",
            "label": "Summarizer",
            "description": "Summarizes a piece of text into a short summary.",
            "defaults": {
                "description": "Summarize the given text.",
                "inputs": [
                    {"name": "text", "type": "str", "description": "Text to summarize", "required": True}
                ],
                "outputs": [
                    {"name": "summary", "type": "str", "description": "Concise summary", "required": True}
                ],
                "prompt": "Summarize the following text concisely:\n{text}",
                "parse_mode": "str",
            },
        },
        {
            "type": "writer",
            "label": "Report Writer",
            "description": "Turns upstream material into a structured report.",
            "defaults": {
                "description": "Write a report from the provided material.",
                "inputs": [
                    {"name": "material", "type": "str", "description": "Source material", "required": True}
                ],
                "outputs": [
                    {"name": "report", "type": "str", "description": "Structured report", "required": True}
                ],
                "prompt": "Write a well-structured report based on:\n{material}",
                "parse_mode": "str",
            },
        },
    ]
}

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Reconcile state a previous process left behind.

    Runs and batches live in memory while they execute; a restart kills the
    threads but leaves their records claiming to be running. Nothing can be
    resumed — the work was LLM calls in flight, and repeating them would bill
    the user twice — so the point is to stop them from hanging forever and say
    what happened instead.
    """
    interrupted = runner.mark_interrupted() + batch_store.mark_interrupted()
    if interrupted:
        print(f"[studio] marked {interrupted} run(s)/batch(es) interrupted by a restart")
    # Schedules are the one thing that *is* resumed: "every day" that stops at
    # the next restart is not a schedule.
    resumed = scheduler.restore()
    if resumed:
        print(f"[studio] resumed schedules for: {', '.join(resumed)}")
    yield


app = FastAPI(title="EvoAgentX Studio", lifespan=_lifespan)
app.include_router(memory_api.router)
from . import mem0_api
app.include_router(mem0_api.router)
if evolve_api is not None:
    app.include_router(evolve_api.router)
app.include_router(workspace_api.router)
app.include_router(custom_tools.router)
app.include_router(chat_api.router)
app.include_router(result_chat.router)
app.include_router(source_collection.router)
from backend.features.data import user_datasets, input_composition
app.include_router(user_datasets.router)
app.include_router(skills_api.router)
app.include_router(export_api.router)
app.include_router(agent_api.router)
from . import harness_api
app.include_router(harness_api.router)

if evolve_api is None:
    def _evolve_unavailable():
        raise HTTPException(
            status_code=503,
            detail=(
                "Workflow evolution requires the optimizer dependencies. "
                "Install the 'optimizers' extra; missing package: "
                f"{_EVOLVE_IMPORT_ERROR}."
            ),
        )

    @app.api_route(
        "/api/evolve{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    )
    def evolve_unavailable(path: str = ""):
        _evolve_unavailable()

    @app.post("/api/graphs/{graph_id}/evolve")
    def graph_evolve_unavailable(graph_id: str):
        _evolve_unavailable()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(projects.router)


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/features")
def list_features():
    overrides = {}
    if _EVOLVE_IMPORT_ERROR:
        overrides["evolve"] = (
            False,
            f"Missing optimizer package: {_EVOLVE_IMPORT_ERROR}",
        )
    return features.catalog(overrides)


@app.get("/api/palette")
def palette():
    return {
        "templates": PALETTE["templates"] + registry.credit_risk_presets(),
        "sources": registry.source_presets(),
    }


@app.get("/api/templates")
def list_templates():
    return {"templates": [{"id": t["id"], "name": t["name"], "description": t["description"]}
                          for t in registry.templates()]}


@app.get("/api/templates/{template_id}")
def get_template(template_id: str):
    for t in registry.templates():
        if t["id"] == template_id:
            return t
    raise HTTPException(status_code=404, detail=f"Template '{template_id}' not found")


def _gray_zone(value) -> tuple | None:
    """Validate an optional [lo, hi] review gray-zone band."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            lo, hi = float(value[0]), float(value[1])
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="review_zone must be [lo, hi] numbers")
        if lo >= hi:
            raise HTTPException(status_code=422, detail="review_zone requires lo < hi")
        return (lo, hi)
    raise HTTPException(status_code=422, detail="review_zone must be [lo, hi]")


@app.get("/api/graphs")
def list_graphs():
    return graph_store.list_graphs()


@app.post("/api/graphs")
def create_graph(body: dict = Body(...)):
    return graph_store.create_graph(name=body.get("name", ""), goal=body.get("goal", ""))


@app.get("/api/graphs/{graph_id}")
def get_graph(graph_id: str):
    graph = graph_store.load_graph(graph_id)
    if graph is None:
        raise HTTPException(status_code=404, detail=f"Graph '{graph_id}' not found")
    ordered, _ = _safe_order(graph)
    return {**graph, "workflow_inputs": graph_store.compute_workflow_inputs(
        ordered, graph.get("edges") or [])}


@app.put("/api/graphs/{graph_id}")
def save_graph(graph_id: str, body: dict = Body(...)):
    if not graph_store.graph_exists(graph_id):
        raise HTTPException(status_code=404, detail=f"Graph '{graph_id}' not found")
    candidate = {
        "id": graph_id,
        "name": body.get("name", graph_id),
        "goal": body.get("goal", ""),
        "tasks": body.get("tasks", []),
        "edges": body.get("edges", []),
    }
    try:
        ordered, workflow_inputs = graph_store.validate_graph(candidate)
        saved = graph_store.save_graph(graph_id, body)
    except graph_store.GraphValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors)
    # The canvas is the source; the workspace is what it compiles to. Keeping
    # them in step on save is what makes the workspace the project rather than
    # a folder that happens to collect run output.
    project_error = _write_project(saved)
    return {**saved, "workflow_inputs": workflow_inputs,
            **({"project_error": project_error} if project_error else {})}


def _write_project(graph: dict) -> str | None:
    """Compile a graph into its workspace; returns why not, if it could not.

    Never raises: a workflow half-built or referring to a tool being edited
    still has to be savable.
    """
    try:
        workspace.write_project(graph)
        return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"


@app.delete("/api/graphs/{graph_id}")
def delete_graph(graph_id: str):
    graph = graph_store.load_graph(graph_id)
    if graph is None: raise HTTPException(404, 'Task not found')
    active = ('running', 'stopping', 'pending', 'queued')
    if any(r.get('status') in active for r in runner.list_runs(graph_id=graph_id)) or any(r.get('status') in active for r in batch_store.list_batches(graph_id=graph_id)):
        raise HTTPException(409, 'Stop active runs before deleting this task.')
    for agent in harness_api.list_agents(graph_id)['agents']:
        if any(s['status'] in ('running', 'stopping') for s in harness_api.list_sessions(graph_id, agent['id'])['sessions']):
            raise HTTPException(409, 'Stop active conversations before deleting this task.')
    scheduler.clear(graph_id)
    watcher.stop_graph_watch(graph_id)
    if not graph_store.delete_graph(graph_id):
        raise HTTPException(status_code=404, detail=f"Graph '{graph_id}' not found")
    return {"ok": True}


def _plan_or_422(graph: dict, start_at, mode: str) -> dict:
    """Compile a plan, turning every way a graph cannot run into a 422."""
    try:
        return run_plan.compile_plan(graph, start_at=start_at, mode=mode)
    except graph_store.GraphValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors)
    except tools_registry.ToolResolveError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.post("/api/graphs/{graph_id}/run-plan")
def plan_run(graph_id: str, body: dict = Body(default={})):
    """What a run would do, before anything is started.

    The dialog reads only this; the run request then presents the plan's id,
    so the inputs shown, the start point offered and the run that executes
    all come from one revision of the graph. Cheap on purpose — no dataset
    is probed — so it can be re-asked on every edit.
    """
    graph = graph_store.load_graph(graph_id)
    if graph is None:
        raise HTTPException(status_code=404, detail=f"Graph '{graph_id}' not found")
    names = body.get("start_at") or []
    if isinstance(names, str):
        names = [n for n in names.split(",") if n.strip()]
    plan = _plan_or_422(graph, names, body.get("mode") or "single")
    # Values from history ride along, so the dialog reads one thing.
    out = {**plan, **_prefill_for(graph_id, plan["inputs"])}
    if body.get("include_records"):
        # The "choose a record" list. Probes the dataset, so only on request.
        try:
            out["records"] = run_plan.records_of(graph, plan)
        except sources.SourceError as e:
            raise HTTPException(status_code=422, detail=str(e))
    return out


@app.post("/api/graphs/{graph_id}/run")
def run_graph(graph_id: str, body: dict = Body(default={})):
    """Start a run — after every check that can be made has been made.

    A refusal here costs nothing. A run that is created and then fails on a
    missing value costs a run id, a record in the history, and sometimes the
    model calls made before it got there.
    """
    graph = graph_store.load_graph(graph_id)
    if graph is None:
        raise HTTPException(status_code=404, detail=f"Graph '{graph_id}' not found")
    start_at = body.get("start_at") or None
    plan = _plan_or_422(graph, start_at, "single")
    warnings = list(plan["warnings"])

    # The plan the user confirmed must be the plan that runs. A different id
    # means the graph changed underneath the dialog; the client re-plans
    # rather than being silently run on the newer version.
    presented = body.get("plan_id")
    if presented and presented != plan["plan_id"]:
        raise HTTPException(status_code=409, detail={
            "code": "plan_stale",
            "message": "The workflow changed since this run was planned. Plan it again.",
            "plan_id": plan["plan_id"],
            "graph_revision": plan["graph_revision"],
        })
    if not presented:
        warnings.append("Starting without a plan_id is deprecated: call "
                        "POST /run-plan first and submit its plan_id.")

    inputs = body.get("inputs") or {}
    problems = run_plan.check_inputs(plan, inputs)
    # Which of the source's records: named, or refused — never assumed.
    record = body.get("record")
    if record is not None:
        source = plan.get("source")
        if source is None:
            problems.append("This workflow has no source node to pick a record from.")
        else:
            try:
                record = int(record)
            except (TypeError, ValueError):
                problems.append(f"record must be a whole number, got {record!r}.")
            else:
                limit = source.get("cardinality")
                if record < 0 or (limit is not None and record >= limit):
                    problems.append(
                        f"record {record} is out of range: '{source['node']}' "
                        f"yields {limit} record(s).")
    if problems:
        raise HTTPException(status_code=422, detail=problems)
    try:
        active_names = {n["name"] for n in plan["nodes"]}
        _validate_source_nodes({**graph,
            "tasks": [t for t in graph.get("tasks", []) if t.get("name") in active_names],
            "edges": [e for e in graph.get("edges", []) if e.get("source") in active_names and e.get("target") in active_names]})
        # Always, even for an empty object: a source-fed workflow has no
        # external inputs, and skipping the preprocessor for `{}` ran it
        # unprocessed. One record, the same shape a batch record has.
        inputs = preprocess.apply(graph, [inputs])[0]
    except sources.SourceError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except preprocess.PreprocessError as e:
        raise HTTPException(status_code=422, detail=str(e))

    run_id = runner.start_run(graph, inputs,
                              gray_zone=_gray_zone(body.get("review_zone")),
                              start_at=start_at,
                              session=body.get("session"),
                              record_index=record,
                              plan_id=plan["plan_id"],
                              graph_revision=plan["graph_revision"])
    return {"run_id": run_id, "plan_id": plan["plan_id"],
            "graph_revision": plan["graph_revision"], "warnings": warnings}


@app.get("/api/graphs/{graph_id}/inputs")
def graph_inputs(graph_id: str, start_at: str | None = Query(default=None)):
    """What a run would ask for, optionally starting from part-way through.

    Starting mid-pipeline turns the upstream nodes' outputs into inputs, and
    those usually already exist: `prefill` carries them from the most recent
    run that produced them, so a re-run of one step does not mean retyping the
    step before it.
    """
    graph = graph_store.load_graph(graph_id)
    if graph is None:
        raise HTTPException(status_code=404, detail=f"Graph '{graph_id}' not found")
    names = [n for n in (start_at or "").split(",") if n.strip()]
    try:
        tasks, edges = graph_store.subgraph_from(
            graph.get("tasks") or [], graph.get("edges") or [], names
        )
    except graph_store.GraphValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors)

    ordered = graph_store.topo_sort_tasks(tasks, edges)
    workflow_inputs = graph_store.compute_workflow_inputs(ordered, edges)

    return {
        "start_at": names,
        "nodes": [t.get("name") for t in graph.get("tasks") or []],
        "workflow_inputs": workflow_inputs,
        **_prefill_for(graph_id, workflow_inputs),
    }


def _prefill_for(graph_id: str, workflow_inputs: list[dict]) -> dict:
    """Values a past run can supply for these inputs.

    Prefers the most recent run that covers *every* needed input. Stopping at
    the first run with any overlap picks up a failed run that never got far
    enough, and the re-run then dies on the one missing value.
    """
    prefill, source_run = {}, None
    wanted = {i["name"] for i in workflow_inputs}
    if wanted:
        for run in runner.list_runs(graph_id=graph_id):
            full = runner.get_run(run.get("run_id")) or {}
            found = {}
            for node in full.get("nodes") or []:
                output = node.get("output")
                if isinstance(output, dict):
                    found.update({k: v for k, v in output.items() if k in wanted})
            found.update({k: v for k, v in (full.get("inputs") or {}).items()
                          if k in wanted and k not in found})
            if len(found) > len(prefill):
                prefill, source_run = found, run.get("run_id")
            if wanted <= set(prefill):
                break
    return {
        "prefill": prefill,
        "prefill_from_run": source_run,
        # What no past run could supply, so the form can point at it instead of
        # letting the run fail on a missing value.
        "prefill_missing": sorted(wanted - set(prefill)),
    }


def _graph_tool_names(graph: dict) -> list[str]:
    return sorted({
        name
        for t in graph.get("tasks", []) or []
        for name in (t.get("tool_names") or [])
    })


def _validate_source_nodes(graph: dict) -> None:
    """Probe local (credit_risk) source nodes; API sources are validated
    statically at save time and fail visibly at run time instead.
    Unconnected source nodes are skipped (the runner ignores them)."""
    wired = {e.get("source") for e in (graph.get("edges") or [])}
    for node in sources.find_source_nodes(graph):
        if node.get("name") not in wired:
            continue
        if (node.get("source") or {}).get("type") in {"credit_risk", "user_dataset"}:
            sources.records_from_source_node(node)


@app.get("/api/runs")
def list_runs(graph_id: str | None = Query(default=None)):
    return runner.list_runs(graph_id=graph_id)


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    run = runner.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    return run


@app.get("/api/graphs/{graph_id}/schedule")
def get_schedule(graph_id: str):
    """This workflow's schedule, if it has one."""
    if not graph_store.graph_exists(graph_id):
        raise HTTPException(status_code=404, detail=f"Graph '{graph_id}' not found")
    return scheduler.status(graph_id)


@app.put("/api/graphs/{graph_id}/schedule")
def put_schedule(graph_id: str, body: dict = Body(...)):
    """Run this workflow on a timer: daily at a time, or every N minutes."""
    graph = graph_store.load_graph(graph_id)
    if graph is None:
        raise HTTPException(status_code=404, detail=f"Graph '{graph_id}' not found")
    try:
        graph_store.validate_graph(graph)
    except graph_store.GraphValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors)
    try:
        return scheduler.set_schedule(graph, body)
    except scheduler.ScheduleError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.delete("/api/graphs/{graph_id}/schedule")
def delete_schedule(graph_id: str):
    return {"ok": True, "removed": scheduler.clear(graph_id)}


@app.post("/api/graphs/{graph_id}/rename")
def rename_graph(graph_id: str, body: dict = Body(...)):
    """Rename a workflow, and its identity with it.

    Separate from saving the graph: renaming is its own action, and going
    through the ordinary save would persist whatever else is on the canvas
    along with it.
    """
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="A workflow needs a name.")
    if not graph_store.graph_exists(graph_id):
        raise HTTPException(status_code=404, detail=f"Graph '{graph_id}' not found")
    try:
        renamed = graph_store.rename_graph(graph_id, name)
    except graph_store.GraphValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors)
    # The generated project names the workflow inside itself, so it is rewritten
    # rather than just carried across.
    _write_project(renamed)
    # The schedule is filed under the id, so it follows rather than stopping.
    scheduler.rename(graph_id, renamed["id"])
    return renamed


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str):
    """Stop a run for real — the model call it is inside is abandoned."""
    return runner.cancel_run(run_id)


@app.post("/api/runs/{run_id}/abandon")
def abandon_run(run_id: str):
    """Give up on a run. It keeps executing — see runner.abandon_run."""
    outcome = runner.abandon_run(run_id)
    if not outcome.get("abandoned") and outcome.get("reason") == "no such run":
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    return outcome


@app.get("/api/sources/credit-risk")
def credit_risk_source(dataset: str | None = None):
    try:
        return sources.credit_risk_info(dataset)
    except sources.SourceError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/sources")
def list_source_types():
    """Config schema for every canvas source type (drives the Inspector form),
    custom toolkits' sources included."""
    from .source_apis import all_source_types

    return {
        "source_types": [
            {"type": type_, **schema} for type_, schema in all_source_types().items()
        ]
    }


@app.post("/api/sources/probe")
def probe_source(body: dict = Body(default={})):
    """Run a source config once and report the fields of its first record.

    For a custom source the outputs are whatever the tool returns; this is
    how the node finds out, instead of the author typing them from memory.
    """
    config = body.get("source") or {}
    try:
        records = sources.records_from_source_node({"name": "probe", "kind": "source",
                                                    "source": config})
    except sources.SourceError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"{type(e).__name__}: {e}")
    first = records[0] if records else {}
    return {"records": len(records), "fields": sorted(first),
            "sample": {k: (str(v)[:80] + "…" if len(str(v)) > 80 else v) for k, v in first.items()}}


@app.get("/api/tools")
def list_tools():
    return {"tools": tools_registry.list_tools()}


def _credit_risk_label(record: dict) -> dict:
    try:
        sample = json.loads(record.get("sample_json") or "{}")
    except ValueError:
        sample = {}
    return {"type": sample.get("type"), "event": (sample.get("label") or {}).get("event"),
            "event_date": (sample.get("label") or {}).get("event_date")}


async def _batch_payload(graph_id: str, request: Request):
    """Work out what a batch would run, without running it.

    Body is either a multipart file upload (JSONL/CSV) or JSON
    {"source": "credit_risk", "split": ..., "n": ..., "seed": ...}.
    Shared by the batch endpoint and its preview so the count shown before
    you start is produced by the same code that then does the work.
    """
    graph = graph_store.load_graph(graph_id)
    if graph is None:
        raise HTTPException(status_code=404, detail=f"Graph '{graph_id}' not found")
    try:
        ordered, workflow_inputs = graph_store.validate_graph(graph)
        tools_registry.validate_tool_names(_graph_tool_names(graph))
    except graph_store.GraphValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors)
    except tools_registry.ToolResolveError as e:
        raise HTTPException(status_code=422, detail=str(e))

    try:
        content_type = request.headers.get("content-type", "")
        review_zone = None
        workers = 2
        # Present only for an evaluation; a plain batch leaves them unset.
        metric = None
        label_key = None
        if content_type.startswith("multipart/form-data"):
            form = await request.form()
            upload = form.get("file")
            if upload is None:
                raise sources.SourceError("Multipart body must include a 'file' field")
            records = sources.parse_upload(upload.filename, await upload.read())
            source = {"type": "upload", "filename": upload.filename}
            if form.get("review_zone"):
                review_zone = json.loads(form["review_zone"])
            if form.get("workers"):
                workers = int(form["workers"])
            metric = form.get("metric") or None
            label_key = form.get("label_key") or None
        else:
            body = await request.json()
            source_name = (body or {}).get("source")
            if source_name == "canvas":
                node = source_collection.node_for(graph)
                config = node["source"]
                if body.get("collection_id"):
                    records, source = source_collection.records_for(graph, node, body["collection_id"])
                elif config.get("type") not in {"credit_risk", "user_dataset"}:
                    raise sources.SourceError("Collect this API source before starting the batch.")
                else:
                    records = sources.records_from_source_node(node)
                    source = {"type": "canvas", "node": node.get("name"),
                              "dataset": config.get("dataset", "contemporary"),
                              **({"dataset_id": config["dataset_id"], "config": config} if config.get("type") == "user_dataset" else {}),
                              "split": config.get("split"), "n": config.get("n", 1),
                              "seed": int(config.get("seed") or 42),
                              "step": config.get("step") or "none"}
            elif source_name == "credit_risk":
                n_raw = body.get("n")
                n_val = int(n_raw) if n_raw is not None else 5  # n=0: entire split
                records = sources.credit_risk_records(
                    split=body.get("split") or None,
                    n=n_val,
                    seed=int(body.get("seed") or 42),
                    step=body.get("step"),
                    **({"dataset": body["dataset"]} if body.get("dataset") else {}),
                )
                source = {
                    "type": "credit_risk",
                    "dataset": body.get("dataset", "contemporary"),
                    "split": body.get("split"),
                    "n": n_val,
                    "seed": int(body.get("seed") or 42),
                }
            else:
                raise sources.SourceError(
                    "JSON body must be {\"source\": \"canvas\"|\"credit_risk\", ...}"
                )
            review_zone = body.get("review_zone")
            workers = int(body.get("workers") or 2)
            metric = body.get("metric") or None
            label_key = body.get("label_key") or None
        from backend.features.execution import provider_batch
        try:
            size = provider_batch.validate(graph, form.get('llm_batch_size') if content_type.startswith('multipart/form-data') else body.get('llm_batch_size'))
        except ValueError as exc:
            raise sources.SourceError(str(exc)) from exc
        if size is not None:
            graph = {**graph, '_llm_batch_size': size}
        # Collected records already contain the reference snapshot taken at collection start.
        if not source.get('collection_id'):
            main = source_collection.node_for(graph) if source.get('type') == 'canvas' else None
            records = input_composition.merge(records, input_composition.snapshot(graph, main))
        # batch records must also cover fields provided by connected canvas
        # source nodes (they are excluded from workflow_inputs by design)
        wired = {e.get("source") for e in (graph.get("edges") or [])}
        mapping_inputs = list(workflow_inputs)
        for node in sources.find_source_nodes(graph):
            if node.get("name") not in wired:
                continue
            for out in node.get("outputs") or []:
                mapping_inputs.append({"name": out["name"], "required": False})
        if source.get('collection_id'):
            mapping_inputs.extend({'name': key, 'required': False} for key in ('sample_id', 'as_of'))
        # Preprocess the raw records: doing it before the mapping is what lets a
        # preprocessor rename or derive the very fields the mapping needs.
        records = preprocess.apply(graph, records)
        # An evaluation is a batch with a metric: pull the expected answers out
        # before mapping, so the label never reaches the workflow as an input.
        labels = None
        if metric and source.get('dataset', 'contemporary') != 'contemporary' and not label_key:
            raise sources.SourceError('This dataset stores outcomes separately. Run without automatic scoring, or provide explicitly labelled evaluation records; daily risk labels are not inferred.')
        if metric and not label_key and all(r.get("sample_json") for r in records[:5]) and records:
            # The credit-risk feed carries its truth in every record; nobody
            # should have to name the field.
            labels = [_credit_risk_label(r) for r in records]
        elif metric:
            records, labels = evaluation.split_labels(records, label_key)
        mapped = sources.map_to_workflow_inputs(records, mapping_inputs)
        if source.get('collection_id'):
            declared = {o['name'] for o in input_composition.all_outputs(graph)}
            if any(declared - record.keys() for record in mapped):
                raise sources.SourceError('Preprocessing removed source outputs. Preserve all source output fields so batch runs can use the collected data.')
    except sources.SourceError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except preprocess.PreprocessError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except evaluation.EvaluationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    return graph, records, mapped, source, review_zone, workers, metric, labels


@app.post("/api/graphs/{graph_id}/run-batch")
async def run_batch(graph_id: str, request: Request):
    """Batch-run a graph over a file upload or a configured sample."""
    graph, _records, mapped, source, review_zone, workers, metric, labels = (
        await _batch_payload(graph_id, request))
    batch_id = batch_store.start_batch(graph, mapped, source,
                                       gray_zone=_gray_zone(review_zone),
                                       workers=workers, metric=metric, labels=labels)
    return {"batch_id": batch_id, "total": len(mapped)}


@app.post("/api/graphs/{graph_id}/run-batch/preview")
async def preview_batch(graph_id: str, request: Request):
    """How many runs this batch would be, before you commit to it.

    Stepping turns one sample into one record per date, so the number of runs
    is not the `n` you typed. Answering that here means the dialog can say so
    up front rather than after 1200 model calls.
    """
    if request.headers.get("content-type", "").startswith("application/json"):
        body = await request.json()
        if body.get("source") == "canvas" and not body.get("collection_id"):
            graph = graph_store.load_graph(graph_id)
            if graph is None:
                raise HTTPException(404, "Task not found")
            try:
                node = source_collection.node_for(graph)
            except sources.SourceError as exc:
                raise HTTPException(422, str(exc)) from exc
            if node["source"].get("type") not in {"credit_risk", "user_dataset"}:
                config = node["source"]
                return {"requires_collection": True, "source": {"node": node["name"],
                        **{key: config.get(key) for key in ('type', 'query', 'days', 'start_date', 'end_date', 'batch_step')}}}
    graph, records, mapped, source, _zone, workers, metric, _labels = (
        await _batch_payload(graph_id, request))
    if not mapped:
        raise HTTPException(status_code=422, detail=(
            "This source yields no records; there is nothing to run."))
    # Every record is a full pass of the workflow: say how many node
    # executions that is, from the same plan the run uses.
    plan = _plan_or_422(graph, None, "batch")
    node_count = len(plan["nodes"])
    # A stepped record carries the sample it came from; without stepping every
    # record is its own sample and the two counts agree.
    by_sample: dict[str, set[str]] = {}
    for r in records:
        if r.get("sample_id") and r.get("as_of"):
            by_sample.setdefault(str(r["sample_id"]), set()).add(str(r["as_of"]))
    # Dates are per sample, and two companies rarely share one: counting them
    # across the whole batch would say "18 dates" for 3 samples of 6.
    per_sample = [len(v) for v in by_sample.values()]
    dates = sorted({d for v in by_sample.values() for d in v})
    return {
        "total": len(mapped),
        "samples": len(by_sample) or len(mapped),
        "dates": [dates[0], dates[-1]] if dates else None,
        "steps": max(per_sample) if per_sample else 1,
        "steps_min": min(per_sample) if per_sample else 1,
        "source": source,
        "workers": workers,
        "metric": metric,
        "nodes": plan["nodes"],
        "skipped": plan["skipped"],
        "node_count": node_count,
        "node_executions": len(mapped) * node_count,
        "fields": sorted({k for r in mapped[:50] for k in r}),
    }


@app.get("/api/metrics")
def list_metrics():
    """Metrics an evaluation can use: the optimizer's built-ins, plus any
    custom tool shaped like one (exactly `prediction` and `label` params)."""
    return {"metrics": evaluation.available_metrics()}


@app.get("/api/batches")
def list_batches(graph_id: str | None = Query(default=None)):
    """Past batches for a workflow, newest first (without their records)."""
    return batch_store.list_batches(graph_id=graph_id)


@app.get("/api/batches/{batch_id}")
def get_batch(batch_id: str):
    batch = batch_store.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"Batch '{batch_id}' not found")
    return batch


@app.get("/api/batches/{batch_id}/compare")
def compare_batches(batch_id: str, baseline: str = Query(...)):
    """How this batch differs from an earlier one, record by record."""
    candidate = batch_store.get_batch(batch_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail=f"Batch '{batch_id}' not found")
    before = batch_store.get_batch(baseline)
    if before is None:
        raise HTTPException(status_code=404, detail=f"Batch '{baseline}' not found")
    return batch_compare.compare(before, candidate)


@app.get("/api/batches/{batch_id}/export")
def export_batch(batch_id: str, format: str = Query(default="csv")):
    """Download a batch's per-record results as CSV or JSON."""
    fmt = (format or "csv").lower()
    if fmt not in ("csv", "json"):
        raise HTTPException(status_code=422,
                            detail=f"Unsupported format '{format}' (use csv or json)")
    batch = batch_store.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"Batch '{batch_id}' not found")
    if fmt == "csv":
        body = batch_export.to_csv(batch)
        media_type = "text/csv; charset=utf-8"
    else:
        body = batch_export.to_json(batch)
        media_type = "application/json"
    name = batch_export.filename(batch, fmt)
    return Response(
        content=body,
        media_type=media_type,
        headers={"content-disposition": f'attachment; filename="{name}"'},
    )


@app.post("/api/batches/{batch_id}/cancel")
def cancel_batch(batch_id: str):
    """Stop a batch starting further items; in-flight items finish."""
    outcome = batch_store.cancel_batch(batch_id)
    if not outcome.get("cancelled") and outcome.get("reason") == "no such batch":
        raise HTTPException(status_code=404, detail=f"Batch '{batch_id}' not found")
    return outcome


@app.post("/api/batches/{batch_id}/evaluate")
def evaluate_batch(batch_id: str, body: dict | None = Body(default=None)):
    """Score a batch from what it holds. Credit-risk batches need nothing
    else (the truth came with the records); any other batch names a metric
    and the field holding the expected answer."""
    from . import evaluation_report
    batch = batch_store.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"Batch '{batch_id}' not found")
    body = body or {}

    def result_of(item):
        run = runner.get_run(item.get("run_id") or "") if item.get("run_id") else None
        return (run or {}).get("result")

    try:
        if body.get("metric"):
            report = evaluation_report.metric_report(
                batch, result_of, body["metric"], body.get("label_key") or "")
        elif evaluation_report.is_credit_risk(batch):
            def text_of(item):
                run = runner.get_run(item.get("run_id") or "") if item.get("run_id") else None
                return " ".join(json.dumps(n.get("output") or {}, ensure_ascii=False)
                                for n in (run or {}).get("nodes") or []
                                if n.get("name") in ("detect", "investigate", "reflect", "decide"))
            # The versioned releases keep outcomes out of the records; the
            # batch remembers which release it ran on, and the truth is
            # looked up there by case id.
            from . import datasets
            labels = datasets.evaluation_labels((batch.get("source") or {}).get("dataset") or "")
            report = evaluation_report.credit_risk_report(batch, result_of, text_of, labels.get)
        else:
            raise HTTPException(status_code=422, detail=(
                "These records carry no ground truth of their own; choose a metric "
                "and the field holding the expected answer."))
    except evaluation.EvaluationError as e:
        raise HTTPException(status_code=422, detail=str(e))
    report["batch_id"] = batch_id
    report["evaluated_at"] = datetime.now(timezone.utc).isoformat()
    report["batch_status"] = batch.get("status")
    batch_store.set_evaluation(batch_id, report)
    return report


@app.get("/api/batches/{batch_id}/evaluation")
def get_batch_evaluation(batch_id: str):
    batch = batch_store.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"Batch '{batch_id}' not found")
    from . import evaluation_report
    return {"evaluation": batch.get("evaluation"),
            "credit_risk": evaluation_report.is_credit_risk(batch)}


@app.post("/api/batches/{batch_id}/resume")
def resume_batch(batch_id: str):
    """Run the records of a stopped batch that did not finish; keep the rest."""
    existing = batch_store.get_batch(batch_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"Batch '{batch_id}' not found")
    graph = graph_store.load_graph(existing.get("graph_id") or "")
    if graph is None:
        raise HTTPException(status_code=409, detail=(
            f"The workflow this batch ran on ('{existing.get('graph_id')}') no longer exists."))
    outcome = batch_store.resume_batch(batch_id, graph)
    if not outcome.get("resumed"):
        raise HTTPException(status_code=409, detail=outcome.get("reason"))
    return outcome


@app.post("/api/graphs/{graph_id}/watch")
def start_watch(graph_id: str):
    """Start watchers for the graph's scheduled (non-ondemand) source nodes."""
    graph = graph_store.load_graph(graph_id)
    if graph is None:
        raise HTTPException(status_code=404, detail=f"Graph '{graph_id}' not found")
    started = watcher.start_graph_watch(graph)
    if not started:
        raise HTTPException(
            status_code=422,
            detail="No source node with a schedule (mode daily/interval) in this graph",
        )
    return {"watching": True, "watchers": started}


@app.delete("/api/graphs/{graph_id}/watch")
def stop_watch(graph_id: str):
    stopped = watcher.stop_graph_watch(graph_id)
    return {"watching": False, "stopped": stopped}


@app.get("/api/graphs/{graph_id}/watch")
def watch_status(graph_id: str):
    return watcher.graph_watch_status(graph_id)


@app.get("/api/review")
def list_reviews(status: str | None = Query(default=None)):
    return review_store.list_reviews(status=status)


@app.post("/api/review/{review_id}")
def resolve_review(review_id: str, body: dict = Body(...)):
    try:
        review = review_store.resolve_review(
            review_id, decision=body.get("decision", ""), note=body.get("note")
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if review is None:
        raise HTTPException(status_code=404, detail=f"Review '{review_id}' not found")
    return review


def _safe_order(graph: dict) -> tuple[list[dict], list[dict]]:
    """Topo-order tasks; fall back to canvas order when edges are invalid."""
    try:
        ordered = graph_store.topo_sort_tasks(
            graph.get("tasks", []) or [], graph.get("edges", []) or []
        )
    except graph_store.GraphValidationError:
        ordered = graph.get("tasks", []) or []
    return ordered, []


# Static hosting of the production frontend build, when present. Mounted last
# so /api/* routes always win.
if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")


def main() -> None:
    """Run the Studio development server."""
    import uvicorn

    uvicorn.run("backend.api.app:app", host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
