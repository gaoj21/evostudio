"""Evolve (MIPRO prompt optimization) API for EvoAgentX Studio.

Runs WorkFlowMiproOptimizer on a canvas graph in a background thread:
canvas tasks become MiproPromptTemplate-backed framework tasks, a small
Benchmark is built from an uploaded JSONL dataset or the credit_risk feed,
and artifacts (optimized graph, before/after metrics, prompt diff) are
persisted to studio/data/evolve/<task_id>/.
"""

import ast
import json
import os
import re
import sys
import threading
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from fastapi import APIRouter, HTTPException, Request

import graphs as graph_store
import sources
from graphs import strip_task, topo_sort_tasks

EVOLVE_DIR = _REPO_ROOT / "studio" / "data" / "evolve"
ENV_PATH = _REPO_ROOT / ".env"

# --- dspy 3.3.0 compatibility shims (copied from credit_risk/optimize_mipro.py)
# evoagentx's MiproOptimizer was written against an older dspy; bridge the
# changed call signatures without forking the optimizer.
import dspy.teleprompt.mipro_optimizer_v2 as _dspy_mipro  # noqa: E402
from evoagentx.optimizers.mipro_optimizer import (  # noqa: E402
    MiproLMWrapper as _MiproLMWrapper,
)
from evoagentx.optimizers.mipro_optimizer import MiproOptimizer as _MiproOptimizer


def _compat_set_hyperparams(self, program, num_trials, minibatch, zeroshot_opt, valset):
    num_trials, valset, minibatch, n_instruct, n_fewshot = (
        _dspy_mipro.MIPROv2._set_hyperparams_from_run_mode(
            self, program, num_trials, minibatch, zeroshot_opt, valset,
            getattr(self, "num_instruct_candidates", None),
            getattr(self, "num_fewshot_candidates", None)))
    if self.auto is not None:
        self.num_instruct_candidates = n_instruct
        self.num_fewshot_candidates = n_fewshot
    return num_trials, valset, minibatch


def _compat_print_auto(self, num_trials, minibatch, valset):
    return _dspy_mipro.MIPROv2._print_auto_run_settings(
        self, num_trials, minibatch, valset,
        getattr(self, "num_fewshot_candidates", None) or 0,
        getattr(self, "num_instruct_candidates", None) or 0)


_MiproOptimizer._set_hyperparams_from_run_mode = _compat_set_hyperparams
_MiproOptimizer._print_auto_run_settings = _compat_print_auto


def _compat_propose_instructions(
    self,
    program,
    trainset,
    demo_candidates,
    view_data_batch_size,
    program_aware_proposer,
    data_aware_proposer,
    tip_aware_proposer,
    fewshot_aware_proposer,
):
    from dspy.propose.grounded_proposer import GroundedProposer
    from dspy.teleprompt.utils import get_signature
    from evoagentx.core.logging import logger as _logger

    _logger.info("==> STEP 2: PROPOSE INSTRUCTION CANDIDATES <==")
    proposer = GroundedProposer(
        program=program,
        trainset=trainset,
        prompt_model=self.prompt_model,
        view_data_batch_size=view_data_batch_size,
        program_aware=program_aware_proposer,
        use_dataset_summary=data_aware_proposer,
        use_task_demos=fewshot_aware_proposer,
        num_demos_in_context=3,
        use_tip=tip_aware_proposer,
        set_tip_randomly=tip_aware_proposer,
        use_instruct_history=False,
        set_history_randomly=False,
        verbose=self.verbose,
        rng=self.rng,
        init_temperature=self.init_temperature,
    )
    instruction_candidates = proposer.propose_instructions_for_program(
        trainset=trainset,
        program=program,
        demo_candidates=demo_candidates,
        trial_logs={},
        N=self.num_instruct_candidates,
    )
    for i, pred in enumerate(program.predicts):
        instruction_candidates[i][0] = get_signature(pred).instructions
    return instruction_candidates


_MiproOptimizer._propose_instructions = _compat_propose_instructions


def _skip_bootstrap_in_zeroshot(self, program, trainset, seed, teacher=None):
    """Zero-shot mode (max_*_demos=0) must not bootstrap at all."""
    if self.max_bootstrapped_demos == 0 and self.max_labeled_demos == 0:
        return None
    return _orig_bootstrap(self, program, trainset, seed, teacher)


_orig_bootstrap = _MiproOptimizer._bootstrap_fewshot_examples
_MiproOptimizer._bootstrap_fewshot_examples = _skip_bootstrap_in_zeroshot

# MiproLMWrapper stores the EvoAgentX LLM object in `self.model`, but dspy
# 3.3.0's LM internals expect a model-NAME string there. Keep the string in
# .model for dspy and route forward() to the real model via .eax_model.
_orig_lm_init = _MiproLMWrapper.__init__


def _patched_lm_init(self, model, *args, **kwargs):
    _orig_lm_init(self, model, *args, **kwargs)
    self.eax_model = model
    self.model = getattr(getattr(model, "config", None), "model",
                         "deepseek/deepseek-v4-flash")


def _patched_lm_forward(self, prompt=None, messages=None, **kwargs):
    response = self.eax_model.generate(prompt=prompt, messages=messages)
    return [response.content]


def _patched_lm_copy(self, **kwargs):
    from copy import deepcopy as _deepcopy
    new_config = _deepcopy(self.eax_model.config)
    new_kwargs = {}
    for key, value in kwargs.items():
        if hasattr(new_config, key):
            setattr(new_config, key, value)
        if (key in self.kwargs) or (not hasattr(self, key)):
            new_kwargs[key] = value
    new_model = self.eax_model.__class__(config=new_config)
    return _MiproLMWrapper(new_model, **new_kwargs)


_MiproLMWrapper.__init__ = _patched_lm_init
_MiproLMWrapper.forward = _patched_lm_forward
_MiproLMWrapper.__call__ = lambda self, prompt=None, messages=None, **kw: self.forward(prompt, messages)
_MiproLMWrapper.copy = _patched_lm_copy
# --- end of shims -----------------------------------------------------------


# ---------------------------------------------------------------------------
# Built-in metrics live in `evaluation`, which is deliberately light: this
# module pulls in the MIPRO optimizer (and optuna), and scoring a batch must
# not have to pay for that.
# ---------------------------------------------------------------------------

from evaluation import (  # noqa: E402
    METRICS,
    _prediction_text,
    _score,
)

def build_benchmark(path: str, input_keys: list[str], metric: str,
                    n_train: int, n_dev: int):
    """A framework Benchmark over a resolved dataset.jsonl of eval records."""
    from evoagentx.benchmark.benchmark import Benchmark

    class StudioBenchmark(Benchmark):
        def _load_data(self):
            with open(self.path, encoding="utf-8") as f:
                records = [json.loads(line) for line in f if line.strip()]
            self._train_data = records[:n_train]
            self._dev_data = records[n_train:n_train + n_dev] or records[:1]

        def get_input_keys(self):
            return list(input_keys)

        def _get_id(self, example):
            return example.get("id")

        def _get_label(self, example):
            return example.get("label")

        def evaluate(self, prediction, label):
            return {"score": _score(metric, prediction, label)}

    return StudioBenchmark(name="studio_evolve", path=path, mode="all")


# ---------------------------------------------------------------------------
# Evolve task engine
# ---------------------------------------------------------------------------

_tasks: dict[str, dict] = {}
_lock = threading.Lock()

_MAX_TEXT = 4000


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task_dir(task_id: str) -> Path:
    return EVOLVE_DIR / task_id


def start_evolve(graph: dict, records: list[dict], metric: str, params: dict) -> str:
    """Start a background MIPRO optimization task; returns the task_id."""
    task_id = uuid.uuid4().hex[:12]
    task_dir = _task_dir(task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    dataset_path = task_dir / "dataset.jsonl"
    with open(dataset_path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    state = {
        "task_id": task_id,
        "graph_id": graph.get("id"),
        "status": "running",
        "stage": "queued",
        "error": None,
        "metric": metric,
        "params": params,
        "dataset": {"path": str(dataset_path), "n_records": len(records)},
        "baseline": None,
        "optimized": None,
        "diff": None,
        "optimized_graph": None,
        "created_at": _utcnow(),
        "finished_at": None,
    }
    with _lock:
        _tasks[task_id] = state
    _persist(state)
    thread = threading.Thread(
        target=_execute_evolve,
        args=(task_id, graph, metric, params, task_dir),
        daemon=True,
    )
    thread.start()
    return task_id


def _execute_evolve(task_id: str, graph_doc: dict, metric: str, params: dict, task_dir: Path) -> None:
    state = _tasks[task_id]
    try:
        import runner as runner_mod
        from evoagentx.agents.agent_manager import AgentManager
        from evoagentx.evaluators import Evaluator
        from evoagentx.optimizers.mipro_optimizer import WorkFlowMiproOptimizer
        from evoagentx.prompts import MiproPromptTemplate
        from evoagentx.workflow.workflow_graph import SequentialWorkFlowGraph, WorkFlowGraph

        llm = runner_mod._make_llm()
        ordered = topo_sort_tasks(graph_doc.get("tasks", []) or [], graph_doc.get("edges", []) or [])
        ordered = [t for t in ordered if t.get("kind") not in ("source", "tool")]  # source/tool nodes are not optimizable
        input_keys = [w["name"] for w in graph_store.compute_workflow_inputs(ordered)]

        # canvas prompt -> optimizable MiproPromptTemplate instruction
        tasks = []
        for t in ordered:
            ft = strip_task(t)
            ft["prompt_template"] = MiproPromptTemplate(instruction=ft.pop("prompt", ""))
            tasks.append(ft)
        graph = SequentialWorkFlowGraph(goal=graph_doc.get("goal", ""), tasks=tasks)

        agent_manager = AgentManager()
        agent_manager.add_agents_from_workflow(graph, llm_config=llm.config)
        benchmark = build_benchmark(
            path=str(task_dir / "dataset.jsonl"),
            input_keys=input_keys,
            metric=metric,
            n_train=int(params.get("n_train") or 1),
            n_dev=int(params.get("n_dev") or 1),
        )
        evaluator = Evaluator(
            llm=llm,
            agent_manager=agent_manager,
            collate_func=lambda ex: ex["inputs"],
            num_workers=1,
        )

        state["stage"] = "baseline"
        baseline = evaluator.evaluate(graph=graph, benchmark=benchmark, eval_mode="dev", update_agents=True)
        state["baseline"] = {"metrics": baseline, "records": _eval_records(evaluator)}

        optimizer = WorkFlowMiproOptimizer(
            graph=graph,
            evaluator=evaluator,
            optimizer_llm=llm,
            max_bootstrapped_demos=int(params.get("max_bootstrapped_demos", 0)),
            max_labeled_demos=int(params.get("max_labeled_demos", 0)),
            auto=None,
            num_candidates=int(params.get("num_candidates", 2)),
            max_steps=int(params.get("max_steps", 2)),
            minibatch=False,
            num_threads=1,
            eval_rounds=1,
            seed=int(params.get("seed", 9)),
            save_path=str(task_dir),
            program_aware_proposer=True,
            data_aware_proposer=False,
            tip_aware_proposer=False,
            fewshot_aware_proposer=False,
            view_data_batch_size=3,
            requires_permission_to_run=False,
            verbose=False,
        )
        state["stage"] = "optimizing"
        optimizer.optimize(dataset=benchmark, metric_name="score")

        best_path = task_dir / "best_program.json"
        if not best_path.is_file():
            raise RuntimeError("optimizer finished without saving best_program.json")
        best_graph = WorkFlowGraph.from_file(str(best_path))

        state["stage"] = "evaluating optimized"
        optimized = evaluator.evaluate(
            graph=best_graph, benchmark=benchmark, eval_mode="dev", update_agents=True
        )
        state["optimized"] = {"metrics": optimized, "records": _eval_records(evaluator)}

        instructions = _extract_instructions(json.loads(best_path.read_text(encoding="utf-8")))
        state["diff"] = [
            {
                "name": t.get("name"),
                "before": t.get("prompt", ""),
                "after": instructions.get(t.get("name"), t.get("prompt", "")),
            }
            for t in ordered
        ]
        optimized_graph = json.loads(json.dumps(graph_doc))  # deep copy
        for t in optimized_graph.get("tasks", []):
            if t.get("name") in instructions:
                t["prompt"] = instructions[t["name"]]
        state["optimized_graph"] = optimized_graph
        state["status"] = "done"
    except Exception:
        state["status"] = "failed"
        state["error"] = traceback.format_exc()
    finally:
        state["stage"] = None
        state["finished_at"] = _utcnow()
        _persist(state)


def _eval_records(evaluator) -> dict:
    records = {}
    for example_id, record in (getattr(evaluator, "_evaluation_records", {}) or {}).items():
        records[str(example_id)] = {
            "prediction": str(record.get("prediction"))[:_MAX_TEXT],
            "label": record.get("label"),
            "metrics": record.get("metrics"),
        }
    return records


def _extract_instructions(data) -> dict:
    """Walk the saved best-program JSON; map agent/node name -> instruction."""
    instructions = {}

    def walk(node):
        if isinstance(node, dict):
            template = node.get("prompt_template")
            if isinstance(template, dict) and template.get("instruction") and node.get("name"):
                instructions[node["name"]] = template["instruction"]
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return instructions


def _persist(state: dict) -> None:
    try:
        with open(_task_dir(state["task_id"]) / "result.json", "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False, default=str)
    except OSError:
        pass


def get_task(task_id: str) -> dict | None:
    with _lock:
        state = _tasks.get(task_id)
        if state is not None:
            return state
    path = _task_dir(task_id) / "result.json"
    if path.is_file():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
    return None


def list_tasks(graph_id: str | None = None) -> list[dict]:
    by_id: dict[str, dict] = {}
    if EVOLVE_DIR.is_dir():
        for path in EVOLVE_DIR.glob("*/result.json"):
            try:
                with open(path, encoding="utf-8") as f:
                    by_id[path.parent.name] = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
    with _lock:
        for task_id, state in _tasks.items():
            by_id[task_id] = state
    tasks = list(by_id.values())
    if graph_id:
        tasks = [t for t in tasks if t.get("graph_id") == graph_id]
    tasks.sort(key=lambda t: t.get("created_at", ""), reverse=True)
    return tasks[:20]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api")


@router.get("/evolve/metrics")
def list_metrics():
    return {"metrics": [{"name": k, "description": v} for k, v in METRICS.items()]}


@router.get("/evolve")
def list_evolve_tasks(graph_id: str | None = None):
    tasks = list_tasks(graph_id=graph_id)
    return [
        {k: t.get(k) for k in (
            "task_id", "graph_id", "status", "stage", "error", "metric",
            "params", "created_at", "finished_at",
        )} | {
            "baseline_score": (t.get("baseline") or {}).get("metrics", {}).get("score"),
            "optimized_score": (t.get("optimized") or {}).get("metrics", {}).get("score"),
        }
        for t in tasks
    ]


@router.get("/evolve/{task_id}")
def get_evolve_task(task_id: str):
    task = get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Evolve task '{task_id}' not found")
    return task


@router.post("/graphs/{graph_id}/evolve")
async def start_evolve_task(graph_id: str, request: Request):
    """Start a MIPRO optimization task. Body is either a multipart dataset
    upload (file = JSONL of {"inputs": {...}, "label": ...}) plus form fields,
    or JSON {"source": "credit_risk", "split"?, "n"?, "seed"?, "metric", ...}."""
    graph = graph_store.load_graph(graph_id)
    if graph is None:
        raise HTTPException(status_code=404, detail=f"Graph '{graph_id}' not found")
    try:
        ordered, workflow_inputs = graph_store.validate_graph(graph)
    except graph_store.GraphValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors)

    try:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith("multipart/form-data"):
            form = await request.form()
            upload = form.get("file")
            if upload is None:
                raise sources.SourceError("Multipart body must include a 'file' field")
            raw = sources.parse_upload(upload.filename, await upload.read())
            records = sources.normalize_eval_records(raw)
            params = {k: v for k, v in form.items() if k != "file"}
            source = {"type": "upload", "filename": upload.filename}
        else:
            body = await request.json() or {}
            if body.get("source") != "credit_risk":
                raise sources.SourceError("JSON body must be {\"source\": \"credit_risk\", ...}")
            records = sources.credit_risk_records(
                split=body.get("split") or None,
                n=int(body.get("n") or 5),
                seed=int(body.get("seed") or 42),
                with_labels=True,
            )
            params = body
            source = {
                "type": "credit_risk",
                "split": body.get("split"),
                "n": int(body.get("n") or 5),
                "seed": int(body.get("seed") or 42),
            }

        metric = params.get("metric") or "exact_match"
        if metric not in METRICS:
            raise sources.SourceError(
                f"Unknown metric '{metric}'. Available: {sorted(METRICS)}"
            )
        # every record's inputs must cover the graph's required workflow inputs
        sources.map_to_workflow_inputs([r["inputs"] for r in records], workflow_inputs)
        params = {
            "n_train": int(params.get("n_train") or max(1, len(records) - 1)),
            "n_dev": int(params.get("n_dev") or 1),
            "num_candidates": int(params.get("num_candidates") or 2),
            "max_steps": int(params.get("max_steps") or 2),
            "max_bootstrapped_demos": int(params.get("max_bootstrapped_demos") or 0),
            "max_labeled_demos": int(params.get("max_labeled_demos") or 0),
            "seed": int(params.get("seed") or 9),
            "source": source,
        }
    except sources.SourceError as e:
        raise HTTPException(status_code=422, detail=str(e))

    task_id = start_evolve(graph, records, metric, params)
    return {"task_id": task_id}


@router.post("/evolve/{task_id}/apply")
def apply_evolve_result(task_id: str):
    """Save an evolve task's optimized graph as a new canvas graph."""
    task = get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Evolve task '{task_id}' not found")
    optimized = task.get("optimized_graph")
    if task.get("status") != "done" or not optimized:
        raise HTTPException(status_code=409, detail="Task has no optimized graph to apply")
    new_graph = graph_store.create_graph(
        name=f"{optimized.get('name', task.get('graph_id'))} (evolved)",
        goal=optimized.get("goal", ""),
    )
    saved = graph_store.save_graph(new_graph["id"], {**optimized, "name": new_graph["name"]})
    return saved
