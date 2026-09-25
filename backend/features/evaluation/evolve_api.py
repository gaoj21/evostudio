"""Evolve (MIPRO prompt optimization) API for EvoAgentX Studio.

Runs WorkFlowMiproOptimizer on a canvas graph in a background thread:
canvas tasks become MiproPromptTemplate-backed framework tasks, a small
Benchmark is built from an uploaded labelled JSON/JSONL/CSV file, and artifacts (optimized graph, before/after metrics, prompt diff) are
persisted to studio-data/evolve/<task_id>/.
"""

import json
import copy
import os
import threading
from contextlib import contextmanager
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from backend.api import graphs as graph_store
from backend.api import sources
from backend.api.graphs import strip_task, topo_sort_tasks
from backend.api.studio_config import data_path
from backend.features.execution import token_usage

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

_REPO_ROOT = Path(__file__).resolve().parents[3]

EVOLVE_DIR = data_path("evolve")
ENV_PATH = _REPO_ROOT / ".env"

# numpy first, whole. dspy pulls optuna, which reaches for numpy.polynomial
# while numpy is still assembling itself when nothing imported numpy before;
# the result is a circular-import error naming numpy.linalg, and Evolve
# reported "missing optimizer package" for a package that was installed.
import numpy  # noqa: E402,F401


# ---------------------------------------------------------------------------
# Stopping a task
#
# `stop_requested` on the task's state is the single flag. It is read at every
# place a task can be interrupted: between records (`_stage`), between the
# examples of an evaluation (`collate`) and — because dspy makes model calls
# of its own, proposing instructions and bootstrapping demos, for minutes at a
# time between those evaluations — before every model call the optimizer makes.
# ---------------------------------------------------------------------------


class EvolveStopped(BaseException):
    """Raised inside a task the user stopped. A BaseException on purpose: the
    framework evaluator catches Exception per example and would carry on."""


def check_stop(state: dict) -> None:
    if state.get("stop_requested"):
        raise EvolveStopped()


# The task the current thread is running, if any. One task per thread, so a
# plain thread-local is the whole of it.
_active = threading.local()


@contextmanager
def stoppable(state: dict):
    """A block in which every model call dspy makes checks this task's Stop."""
    previous = getattr(_active, "state", None)
    _active.state = state
    try:
        yield
    finally:
        _active.state = previous


def _check_active_stop() -> None:
    state = getattr(_active, "state", None)
    if state is not None:
        check_stop(state)


# --- dspy 3.3.0 compatibility shims
# evoagentx's MiproOptimizer was written against an older dspy; bridge the
# changed call signatures without forking the optimizer.
import dspy.teleprompt.mipro_optimizer_v2 as _dspy_mipro  # noqa: E402
from evoagentx.optimizers.mipro_optimizer import (  # noqa: E402
    MiproLMWrapper as _MiproLMWrapper,
)
from evoagentx.optimizers.mipro_optimizer import MiproOptimizer as _MiproOptimizer  # noqa: E402


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
    # dspy wants a model-NAME string here; the bridge's config carries the id
    # the deployment's provider is configured with. No model is named here.
    self.model = str(getattr(getattr(model, "config", None), "model", None) or "studio")


def _patched_lm_forward(self, prompt=None, messages=None, **kwargs):
    # The optimizer's own calls — instruction proposals, demo bootstrapping —
    # go through here. Without this check Stop waited for the next evaluation.
    _check_active_stop()
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
    # The copied config carries the run's usage key, so a model built from it
    # reports to the same hook as the original — nothing to re-attach.
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

from backend.api.evaluation import (  # noqa: E402
    METRICS,
    _score,
    available_metrics,
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
# How much to try. Three names instead of six numbers: MIPRO's candidates ×
# steps decide how many full evaluations of the dev set it will pay for, and
# nobody starting an optimization wants to reason about that.
# ---------------------------------------------------------------------------

PRESETS = {
    "quick": {"num_candidates": 2, "max_steps": 2,
              "label": "Quick", "blurb": "2 candidate prompts, 2 rounds. A first look; minutes."},
    "standard": {"num_candidates": 4, "max_steps": 4,
                 "label": "Standard", "blurb": "4 candidates, 4 rounds. The usual choice."},
    "thorough": {"num_candidates": 6, "max_steps": 8,
                 "label": "Thorough", "blurb": "6 candidates, 8 rounds. Leave it running."},
}


def resolve_params(raw: dict, n_records: int, llm_nodes: list[str]) -> dict:
    """The optimizer's settings from what the form sent.

    A preset fills candidates and steps; a dev share (default 30%) splits
    the records; `nodes` names which prompts may change (default: all LLM
    nodes). Anything sent explicitly wins over the preset, so the Advanced
    fields still mean what they say.
    """
    mode = raw.get("mode", "evolve_evaluate")
    if mode not in ("evaluate", "evolve_evaluate"):
        raise sources.SourceError("mode must be evaluate or evolve_evaluate")
    if mode == "evaluate":
        if n_records < 1:
            raise sources.SourceError("Evaluation needs at least one record.")
        return {"mode": mode, "n_train": 0, "n_dev": n_records, "nodes": []}
    preset = str(raw.get("preset") or "quick").lower()
    if preset not in PRESETS:
        raise sources.SourceError(f"Unknown preset '{preset}'. Choose one of {sorted(PRESETS)}.")
    chosen = PRESETS[preset]
    n = max(0, int(n_records))
    n_dev_raw = raw.get("n_dev")
    n_dev = int(n_dev_raw) if n_dev_raw not in (None, "") else max(1, round(n * 0.3))
    n_dev = max(1, min(n_dev, max(1, n - 1))) if n > 1 else 1
    n_train_raw = raw.get("n_train")
    n_train = int(n_train_raw) if n_train_raw not in (None, "") else max(1, n - n_dev)
    nodes = raw.get("nodes")
    if isinstance(nodes, str):
        nodes = [x.strip() for x in nodes.split(",") if x.strip()]
    nodes = [x for x in (nodes or []) if x in llm_nodes] or list(llm_nodes)

    def num(key, default):
        value = raw.get(key)
        return int(value) if value not in (None, "") else int(default)

    return {
        "mode": mode,
        "preset": preset,
        "n_train": n_train,
        "n_dev": n_dev,
        "num_candidates": num("num_candidates", chosen["num_candidates"]),
        "max_steps": num("max_steps", chosen["max_steps"]),
        "max_bootstrapped_demos": num("max_bootstrapped_demos", 0),
        "max_labeled_demos": num("max_labeled_demos", 0),
        "seed": num("seed", 9),
        "nodes": nodes,
    }


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
    graph, params, records = copy.deepcopy(graph), copy.deepcopy(params), copy.deepcopy(records)
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
        # Where it is and since when: the panel shows this as a progress line.
        "stages": [],
        "source": params.get("source"),
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


def _stage(state: dict, name: str) -> None:
    check_stop(state)
    state["stage"] = name
    state.setdefault("stages", []).append({"stage": name, "at": _utcnow()})
    _persist(state)


def _attach_agents(agent_manager, state: dict) -> None:
    """Count what every agent's model reports, as each call returns.

    An agent rebuilt for a candidate is built from the config Evolve handed
    over, which already carries this task's usage key; this covers a rebuild
    that was handed none.
    """
    key = state.get("_usage_key")
    for agent in getattr(agent_manager, "agents", None) or []:
        if getattr(agent, "llm", None) is not None:
            token_usage.attach_usage(agent.llm, key)


def _finish(state: dict) -> None:
    """Mark the task done — unless Stop arrived while the last step ran, in
    which case what that step produced is not a result."""
    check_stop(state)
    state["status"] = "done"


def _execute_evolve(task_id: str, graph_doc: dict, metric: str, params: dict, task_dir: Path) -> None:
    """Run one task on this thread, with its Stop visible to the optimizer."""
    with stoppable(_tasks[task_id]):
        try:
            _run_task(task_id, graph_doc, metric, params, task_dir)
        finally:
            token_usage.release_usage(_tasks[task_id])


def _run_task(task_id: str, graph_doc: dict, metric: str, params: dict, task_dir: Path) -> None:
    state = _tasks[task_id]
    try:
        if (params.get('source') or {}).get('type') == 'canvas':
            from backend.features.evaluation import canvas_evolution
            if params.get('load_input'):
                _stage(state, 'reading the Input')
                records = canvas_evolution.load_rows(graph_doc)
                check_stop(state)
                with open(task_dir / 'dataset.jsonl', 'w', encoding='utf-8') as f:
                    for record in records:
                        f.write(json.dumps(record, ensure_ascii=False) + '\n')
                params['n_dev'] = len(records)
                state['params'] = {**state['params'], 'n_dev': len(records)}
                state['dataset'] = {**(state.get('dataset') or {}), 'n_records': len(records)}
                _persist(state)
            else:
                records = [json.loads(line) for line in (task_dir / 'dataset.jsonl').read_text().splitlines() if line.strip()]
            state['execution_graph'] = graph_doc
            canvas_evolution.execute(state, graph_doc, records, params, lambda text: _stage(state, text))
            _finish(state)
            return
        if (params.get("source") or {}).get("type") in ("saved_batch", "saved_run"):
            from backend.api import saved_result_evolution as saved
            records = [json.loads(line) for line in (task_dir / "dataset.jsonl").read_text().splitlines() if line.strip()]
            _stage(state, "scoring saved results")
            if params.get('evaluator'):
                from backend.features.evaluation.canvas_evolution import score_saved
                state['baseline'] = score_saved(graph_doc, records, params['evaluator'])
            else:
                state["baseline"] = saved.evaluate(records, metric, params["source"])
            check_stop(state)
            # Saved results are only evaluated: Evolve replays the canvas Input.
            _finish(state)
            return
        from backend.api import runner as runner_mod
        from evoagentx.agents.agent_manager import AgentManager
        from evoagentx.evaluators import Evaluator
        from evoagentx.optimizers.mipro_optimizer import WorkFlowMiproOptimizer
        from evoagentx.prompts import MiproPromptTemplate
        from evoagentx.workflow.workflow_graph import SequentialWorkFlowGraph, WorkFlowGraph

        llm = runner_mod._make_llm(usage_key=token_usage.usage_key(state))
        ordered = topo_sort_tasks(graph_doc.get("tasks", []) or [], graph_doc.get("edges", []) or [])
        ordered = [t for t in ordered if t.get("kind") not in ("source", "tool")]  # source/tool nodes are not optimizable
        input_keys = [w["name"] for w in graph_store.compute_workflow_inputs(ordered)]

        # canvas prompt -> optimizable MiproPromptTemplate instruction, for
        # the nodes chosen; the others keep their prompt as it is.
        evaluation_only = params.get("mode") == "evaluate"
        chosen = set() if evaluation_only else set(params.get("nodes") or [t.get("name") for t in ordered])
        tasks = []
        for t in ordered:
            ft = strip_task(t)
            if t.get("name") in chosen:
                ft["prompt_template"] = MiproPromptTemplate(instruction=ft.pop("prompt", ""))
            tasks.append(ft)
        graph = SequentialWorkFlowGraph(goal=graph_doc.get("goal", ""), tasks=tasks)

        # The same tools a run would have: a node that calls a toolkit cannot
        # be evaluated without it.
        from backend.api import tools_registry, workspace as workspace_mod
        tools = tools_registry.resolve_tools(
            sorted({n for t in ordered for n in (t.get("tool_names") or [])}),
            workspace_dir=workspace_mod.files_dir(graph_doc.get("id", "graph")),
        ) or None
        agent_manager = AgentManager()
        agent_manager.add_agents_from_workflow(graph, llm_config=llm.config, tools=tools)
        _attach_agents(agent_manager, state)
        # The evaluator and the optimizer rebuild the agents from the graph
        # on every candidate (update_agents_from_workflow) and pass no tools
        # when they do; an agent that names a toolkit then fails to build
        # ("Must provide the following tools"). Hand the same tools to every
        # rebuild.
        _update = agent_manager.update_agents_from_workflow

        def _update_with_tools(workflow_graph, llm_config=None, tools=None, **kw):
            result = _update(workflow_graph=workflow_graph, llm_config=llm_config,
                             tools=tools if tools is not None else base_tools, **kw)
            _attach_agents(agent_manager, state)
            return result
        base_tools = tools
        agent_manager.update_agents_from_workflow = _update_with_tools
        benchmark = build_benchmark(
            path=str(task_dir / "dataset.jsonl"),
            input_keys=input_keys,
            metric=metric,
            n_train=int(params.get("n_train", 1)),
            n_dev=int(params.get("n_dev") or 1),
        )
        def collate(example):
            check_stop(state)          # every example: Stop takes effect within one
            return example["inputs"]

        evaluator = Evaluator(
            llm=llm,
            agent_manager=agent_manager,
            collate_func=collate,
            num_workers=1,
        )

        _stage(state, "evaluating" if evaluation_only else "baseline")
        baseline = evaluator.evaluate(graph=graph, benchmark=benchmark, eval_mode="dev", update_agents=True)
        state["baseline"] = {"metrics": baseline, "records": _eval_records(evaluator)}

        if evaluation_only:
            _finish(state)
            return

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
        _stage(state, "optimizing")
        optimizer.optimize(dataset=benchmark, metric_name="score")

        best_path = task_dir / "best_program.json"
        if not best_path.is_file():
            raise RuntimeError("optimizer finished without saving best_program.json")
        best_graph = WorkFlowGraph.from_file(str(best_path))

        _stage(state, "evaluating optimized")
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
                "changed": instructions.get(t.get("name"), t.get("prompt", "")) != t.get("prompt", ""),
                "optimized": t.get("name") in chosen,
            }
            for t in ordered
        ]
        optimized_graph = json.loads(json.dumps(graph_doc))  # deep copy
        for t in optimized_graph.get("tasks", []):
            if t.get("name") in instructions:
                t["prompt"] = instructions[t["name"]]
        state["optimized_graph"] = optimized_graph
        _finish(state)
    except EvolveStopped:
        state["status"] = "stopped"
        state["error"] = None
    except Exception:
        state["status"] = "failed"
        state["error"] = traceback.format_exc()
    finally:
        state.pop("current_run", None)
        state.pop("current_batch", None)
        state["stage"] = None
        state["finished_at"] = _utcnow()
        try:
            started = datetime.fromisoformat(state["created_at"])
            state["elapsed_seconds"] = round((datetime.now(timezone.utc) - started).total_seconds())
        except (KeyError, ValueError):
            pass
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
        # Internal handles (the usage hook's key) are not part of the record.
        saved = {k: v for k, v in state.items() if not k.startswith("_")}
        with open(_task_dir(state["task_id"]) / "result.json", "w", encoding="utf-8") as f:
            json.dump(saved, f, indent=2, ensure_ascii=False, default=str)
    except OSError:
        pass


def _live(state: dict) -> dict:
    """The task with what its in-flight Run has reported so far.

    A canvas Run's usage is settled into the task in the same step that
    clears `current_run`, so a snapshot holds one or the other.
    """
    snapshot = {k: v for k, v in state.items() if not k.startswith("_")}
    if not snapshot.get("current_run") and not snapshot.get("current_batch"):
        return snapshot
    from backend.features.execution.token_usage import combined
    if snapshot.get("current_batch"):
        # A candidate replay: the batch it runs as, usage settled so far.
        from backend.api import batch
        running = batch.get_batch(snapshot["current_batch"]) or {}
    else:
        from backend.api import runner
        running = runner.get_run(snapshot["current_run"]) or {}
    usage = combined(snapshot.get("token_usage"), running.get("token_usage"))
    return {**snapshot, "token_usage": usage} if usage != snapshot.get("token_usage") else snapshot


def get_task(task_id: str) -> dict | None:
    with _lock:
        state = _tasks.get(task_id)
        if state is not None:
            return _live(state)
    path = _task_dir(task_id) / "result.json"
    if path.is_file():
        try:
            with open(path, encoding="utf-8") as f:
                return _orphaned(json.load(f))
        except (json.JSONDecodeError, OSError):
            return None
    return None


def _orphaned(state: dict) -> dict:
    """A task saved as running that this process is not running was cut off
    by a restart; say so instead of showing it as running forever."""
    if state.get("status") == "running":
        state = {**state, "status": "interrupted", "stage": None,
                 "error": "The server restarted while this task was running. Start it again."}
    return state


def list_tasks(graph_id: str | None = None) -> list[dict]:
    # A task cut off by a restart leaves its candidates' scratch stores
    # behind; nothing reads them again, so they go here. Only the scratch
    # namespace is touched, and never a task that is still running.
    from backend.features.evaluation.canvas_evolution import discard_candidate_stores
    with _lock:
        running = [t for t, s in _tasks.items() if s.get("status") == "running"]
    try:
        discard_candidate_stores(keep=running)
    except OSError:
        pass
    by_id: dict[str, dict] = {}
    if EVOLVE_DIR.is_dir():
        for path in EVOLVE_DIR.glob("*/result.json"):
            try:
                with open(path, encoding="utf-8") as f:
                    by_id[path.parent.name] = _orphaned(json.load(f))
            except (json.JSONDecodeError, OSError):
                continue
    with _lock:
        for task_id, state in _tasks.items():
            by_id[task_id] = _live(state)
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
    return {"metrics": available_metrics()}


@router.get("/evolve/presets")
def list_presets():
    return {"presets": [{"name": k, **v} for k, v in PRESETS.items()]}


@router.get("/evolve")
def list_evolve_tasks(graph_id: str | None = None):
    tasks = list_tasks(graph_id=graph_id)
    return [
        {k: t.get(k) for k in (
            "task_id", "graph_id", "status", "stage", "error", "metric",
            "params", "created_at", "finished_at", "elapsed_seconds", "source", "token_usage",
            # A task still running because Stop has not reached its next
            # checkpoint yet is not simply "running"; the list says so too.
            "stop_requested",
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


@router.post("/graphs/{graph_id}/evolve/preview")
async def preview_saved_evaluation(graph_id: str, request: Request):
    from backend.api import saved_result_evolution as saved
    body = await request.json()
    if body.get("source") not in ("saved_batch", "saved_run"):
        raise HTTPException(status_code=422, detail="Select saved results to preview.")
    try:
        records, selection = saved.resolve(graph_id, body)
        if body.get('evaluator'):
            from backend.features.evaluation.canvas_evolution import select
            from backend.features.evaluation.evaluator_tools import evaluator_name
            select(graph_store.load_graph(graph_id) or {}, body['evaluator'])
            return {**selection, 'suggested_metric': 'canvas:' + evaluator_name(body['evaluator']),
                    'scoring': {'scored': None, 'unscored': None, 'total': len(records)},
                    'note': 'The workflow\'s evaluation code will score saved outputs. Preview does not run it.'}
        metric = body.get('metric') or saved.default_metric(selection)
        if not any(m['name'] == metric for m in available_metrics()):
            raise sources.SourceError('Choose an available metric.')
        eligible = sum(r.get('status') == 'success' and r.get('label') is not None for r in records)
        return {**selection, 'suggested_metric': saved.default_metric(selection),
                'scoring': {'scored': eligible, 'unscored': len(records) - eligible, 'total': len(records)}}
    except sources.SourceError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/graphs/{graph_id}/evolve")
async def start_evolve_task(graph_id: str, request: Request):
    """Start an evaluation or optimization task.

    Body is either a multipart labelled-file upload (records of
    {"inputs": {...}, "label": ...}) plus form fields, optimized with MIPRO;
    or JSON {"source": "canvas"|"saved_batch"|"saved_run", ...}, scored by a
    canvas evaluator or a metric."""
    graph = graph_store.load_graph(graph_id)
    if graph is None:
        raise HTTPException(status_code=404, detail=f"Graph '{graph_id}' not found")
    content_type = request.headers.get("content-type", "")
    body = None if content_type.startswith("multipart/form-data") else (await request.json() or {})
    if body and body.get("source") in ("saved_batch", "saved_run"):
        # Historical predictions do not depend on today's canvas wiring.
        ordered, workflow_inputs = graph.get("tasks", []), []
    else:
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
            if body.get('source') == 'canvas':
                from backend.features.evaluation import canvas_evolution
                # Checked here; the Input is read by the task, where it shows
                # as a stage and Stop reaches it, not while the request waits.
                params = canvas_evolution.settings(graph, body)
                return {'task_id': start_evolve(graph, [], 'canvas:' + params['evaluator'], {**params, 'load_input': True})}
            if body.get("source") in ("saved_batch", "saved_run"):
                from backend.api import saved_result_evolution as saved
                records, source = saved.resolve(graph_id, body)
                metric = body.get("metric") or saved.default_metric(source)
                if body.get('evaluator'):
                    from backend.features.evaluation.canvas_evolution import select
                    from backend.features.evaluation.evaluator_tools import evaluator_name
                    select(graph, body['evaluator'])
                    metric = 'canvas:' + evaluator_name(body['evaluator'])
                elif not any(m["name"] == metric for m in available_metrics()):
                    raise sources.SourceError("Choose an available metric.")
                mode = body.get("mode", "evaluate")
                if mode != "evaluate":
                    # Proposals from saved traces are never validated: Evolve
                    # replays the canvas Input, with a held-out part.
                    raise sources.SourceError("Saved results can only be evaluated. Evolve from the canvas Input: it replays the workflow and validates on held-out data.")
                available = [t["name"] for t in ordered if t.get("kind") not in ("source", "tool")]
                chosen = [] if mode == "evaluate" else body.get("nodes", available)
                if not isinstance(chosen, list) or any(n not in available for n in chosen):
                    raise sources.SourceError("Choose valid nodes to improve.")
                if mode != "evaluate" and not chosen:
                    raise sources.SourceError("Choose at least one prompt to improve.")
                if not isinstance(body.get("share_labels", False), bool):
                    raise sources.SourceError("share_labels must be true or false.")
                params = {"mode": mode, "source": source, "nodes": chosen, "n_dev": len(records), "n_train": 0, "evaluator": body.get("evaluator"),
                          "label_key": body.get("label_key") or None, "share_labels": body.get("share_labels", False)}
                from backend.features.evaluation.evaluator_tools import evaluator_name
                params["evaluator"] = evaluator_name(params["evaluator"]) if params.get("evaluator") else None
                return {"task_id": start_evolve(graph, records, metric, params)}
            raise sources.SourceError(
                "Choose the data: the canvas Input with an evaluator, saved results, or an uploaded labelled file.")

        metric = params.get("metric") or "exact_match"
        if not any(m["name"] == metric for m in available_metrics()):
            raise sources.SourceError(
                f"Unknown metric '{metric}'. Available: {sorted(m['name'] for m in available_metrics())}"
            )
        if params.get("mode") != "evaluate" and len(records) < 2:
            raise sources.SourceError("An optimization needs at least 2 records: one to learn from, one to judge by.")
        # every record's inputs must cover the graph's required workflow inputs
        sources.map_to_workflow_inputs([r["inputs"] for r in records], workflow_inputs)
        llm_nodes = [t.get("name") for t in ordered if t.get("kind") not in ("source", "tool")]
        params = {**resolve_params(params, len(records), llm_nodes), "source": source}
    except sources.SourceError as e:
        raise HTTPException(status_code=422, detail=str(e))

    task_id = start_evolve(graph, records, metric, params)
    return {"task_id": task_id}


@router.post("/evolve/{task_id}/stop")
def stop_evolve_task(task_id: str):
    """Stop a running task at its next record; the in-flight run, if any, is
    cancelled. Nothing it produced is applied."""
    with _lock:
        state = _tasks.get(task_id)
    if state is None:
        if get_task(task_id) is None:
            raise HTTPException(status_code=404, detail=f"Evolve task '{task_id}' not found")
        raise HTTPException(status_code=409, detail="This task is not running.")
    if state.get("status") != "running":
        raise HTTPException(status_code=409, detail="This task is not running.")
    state["stop_requested"] = True
    batch_id = state.get("current_batch")
    if batch_id:
        from backend.api import batch
        try:
            batch.cancel_batch(batch_id)   # its records stop now, not at the next poll
        except Exception:
            pass
    run_id = state.get("current_run")
    if run_id:
        from backend.api import runner
        try:
            # The candidate replay publishes the id before it starts the run,
            # so a stop landing in that window must be held for it rather than
            # refused — otherwise that record runs in full after Stop.
            runner.cancel_run(run_id, expected=True)
        except Exception:
            pass
    _persist(state)
    return {"stopping": True}


@router.post("/evolve/{task_id}/apply")
def apply_evolve_result(task_id: str, mode: str = "new"):
    """Take the optimized prompts into a workflow.

    `mode=new` saves them as a new workflow beside the original, as before.
    `mode=replace` writes them into the workflow that was optimized — after
    saving a copy of it as "<name> (before evolve)", so nothing is lost.
    """
    task = get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Evolve task '{task_id}' not found")
    optimized = task.get("optimized_graph")
    if task.get("status") != "done" or not optimized:
        raise HTTPException(status_code=409, detail="Task has no optimized graph to apply")
    if mode not in ("new", "replace"):
        raise HTTPException(status_code=422, detail="mode must be 'new' or 'replace'")
    if mode == "new":
        new_graph = graph_store.create_graph(
            name=f"{optimized.get('name', task.get('graph_id'))} (evolved)",
            goal=optimized.get("goal", ""),
        )
        return graph_store.save_graph(new_graph["id"], {**optimized, "name": new_graph["name"]})

    graph_id = task.get("graph_id")
    current = graph_store.load_graph(graph_id or "")
    if current is None:
        raise HTTPException(status_code=409, detail=(
            f"The workflow '{graph_id}' no longer exists; save as new instead."))
    backup = graph_store.create_graph(
        name=f"{current.get('name', graph_id)} (before evolve)", goal=current.get("goal", ""))
    graph_store.save_graph(backup["id"], {**current, "id": backup["id"], "name": backup["name"]})
    # Only the prompts Evolve changed move: the workflow as it is now, with
    # the optimized instructions, rather than the snapshot the optimizer ran
    # on — a prompt edited since the task started is not put back.
    # (A task saved before diffs were kept moves every prompt, as it did.)
    changed = None if task.get("diff") is None else {d.get("name") for d in task["diff"] if d.get("changed")}
    new_prompts = {t.get("name"): t.get("prompt") for t in optimized.get("tasks") or []
                   if changed is None or t.get("name") in changed}
    for t in current.get("tasks") or []:
        if t.get("name") in new_prompts and new_prompts[t["name"]] is not None:
            t["prompt"] = new_prompts[t["name"]]
    saved = graph_store.save_graph(graph_id, {**current, "name": current.get("name")})
    return {**saved, "backup_id": backup["id"], "backup_name": backup["name"]}
