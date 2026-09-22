"""
Workflow search with the skill-style evolution machinery.

A skill's instructions are a prompt; a workflow's definition is also just a
text artifact — the JSON config of a ``WorkFlowGraph``. This module applies
the same register → mutate → evaluate → keep-best → save loop to workflows:

- :class:`WorkflowProgram` holds the workflow as JSON text (the evolvable
  artifact), with ``save``/``load`` and versioned backups like skills.
- :class:`WorkflowSearchOptimizer` proposes structural variants with an LLM
  (add/remove/rewire nodes, adjust prompts), validates them, evaluates them
  on a benchmark through an ``Evaluator``, and keeps the best (hill climbing).
- :func:`make_workflow_search_optimizer` wires up a ready-to-use optimizer.

Example::

    from evoagentx.skills import (
        ListBenchmark, WorkflowProgram, make_workflow_search_optimizer,
    )

    program = WorkflowProgram(graph=my_workflow_graph)
    optimizer = make_workflow_search_optimizer(
        program,
        executor_llm=executor_llm,
        optimizer_llm=optimizer_llm,
        collate_func=lambda x: {"problem": x["problem"]},
        max_rounds=10,
    )
    result = optimizer.optimize(ListBenchmark("my_eval", data))
    program.save("best_workflow.json", backup=True)
"""

import json
import os
import shutil
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from ..core.logging import logger
from ..core.module_utils import parse_json_from_llm_output
from ..workflow.workflow_graph import WorkFlowGraph

WORKFLOW_SEARCH_PROPOSER_PROMPT = """You are an expert designer of LLM agent workflows.

You are given the current workflow definition (JSON) and its evaluation history. Propose ONE improved workflow.

Current workflow JSON:
```json
{workflow}
```

Evaluation history (higher scores are better):
{history}
{feedback}
Guidelines:
- Modify the structure (add, remove, reorder or rewire nodes; adjust node inputs/outputs) and/or improve the agents' prompts and instructions.
- Keep the exact same JSON schema as the current workflow.
- Every node input must be satisfiable from the workflow's initial inputs or another node's output, and the initial input names must stay unchanged.
- The graph must remain a DAG with at least one node and exactly one end node.
{failures}
Return ONLY the complete improved workflow JSON. No explanations, no markdown fences.
"""


class WorkflowProgram:
    """A program whose state is a workflow definition (JSON text).

    The ``workflow_text`` attribute is the evolvable artifact: it is the JSON
    config of a ``WorkFlowGraph`` (as produced by ``graph.get_config()``).
    Optimizers rewrite it; :meth:`build_graph` turns it back into an
    executable graph.

    Args:
        graph: A ``WorkFlowGraph`` to initialize from.
        workflow_text: A JSON string of a workflow config. Ignored if ``graph``
            is given.
    """

    def __init__(self, graph: Optional[WorkFlowGraph] = None, workflow_text: Optional[str] = None):
        if graph is None and workflow_text is None:
            raise ValueError("Provide either `graph` or `workflow_text`.")
        if graph is not None:
            workflow_text = json.dumps(graph.get_config(), ensure_ascii=False, indent=2)
        self.workflow_text = workflow_text

    @classmethod
    def from_file(cls, path: str) -> "WorkflowProgram":
        """Load a workflow program from a JSON file saved by :meth:`save`."""
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "workflow" in data:
            data = data["workflow"]
        return cls(workflow_text=json.dumps(data, ensure_ascii=False, indent=2))

    def build_graph(self) -> WorkFlowGraph:
        """Parse the current workflow text into an executable ``WorkFlowGraph``."""
        config = json.loads(self.workflow_text)
        return WorkFlowGraph.from_dict(config)

    def update_from_graph(self, graph: WorkFlowGraph):
        """Replace the workflow text with the given graph's config."""
        self.workflow_text = json.dumps(graph.get_config(), ensure_ascii=False, indent=2)

    def save(self, path: str, backup: bool = False) -> str:
        """Save the workflow definition to a JSON file.

        Args:
            path: Target JSON file path.
            backup: If True, archive the existing file into a ``.versions``
                sub-directory next to ``path`` before overwriting it.

        Returns:
            The path that was written.
        """
        if backup and os.path.exists(path):
            versions_dir = os.path.join(os.path.dirname(os.path.abspath(path)), ".versions")
            os.makedirs(versions_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            base_name = os.path.splitext(os.path.basename(path))[0]
            backup_path = os.path.join(versions_dir, f"{base_name}_{timestamp}.json")
            shutil.copy2(path, backup_path)
            logger.info(f"Backed up previous workflow version to '{backup_path}'.")
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"workflow": json.loads(self.workflow_text)}, f, ensure_ascii=False, indent=2)
        return path

    def load(self, path: str):
        """Load the workflow definition from a JSON file saved by :meth:`save`."""
        loaded = WorkflowProgram.from_file(path)
        self.workflow_text = loaded.workflow_text


class WorkflowSearchOptimizer:
    """Searches the workflow space by evolving a workflow's JSON definition with an LLM.

    Each round, the optimizer LLM proposes a structural variant of the current
    best workflow (based on the score history and feedback from invalid
    proposals). Valid variants are evaluated on a benchmark through an
    ``Evaluator``; the search hill-climbs on the best-scoring workflow.

    Args:
        program: The :class:`WorkflowProgram` to optimize.
        evaluator: An ``evoagentx.evaluators.Evaluator`` (or any object with a
            compatible ``evaluate(graph=..., benchmark=..., eval_mode=...,
            update_agents=True) -> dict`` method).
        optimizer_llm: The LLM used to propose workflow variants.
        max_rounds: Maximum number of search rounds (proposals).
        objective_metric: The metric key in the evaluator's result to optimize. If None,
            the mean of all numeric metrics is used.
        maximize: Whether to maximize (default) or minimize the metric.
        eval_mode: The benchmark split used for evaluation during search.
        eval_config: Extra kwargs forwarded to ``evaluator.evaluate``.
    """

    def __init__(
        self,
        program: WorkflowProgram,
        evaluator,
        optimizer_llm,
        max_rounds: int = 10,
        objective_metric: Optional[str] = None,
        maximize: bool = True,
        eval_mode: str = "dev",
        eval_config: Optional[dict] = None,
    ):
        self.program = program
        self.evaluator = evaluator
        self.optimizer_llm = optimizer_llm
        self.max_rounds = max_rounds
        self.objective_metric = objective_metric
        self.maximize = maximize
        self.eval_mode = eval_mode
        self.eval_config = eval_config or {}
        self.history: List[Dict[str, Any]] = []
        self._last_failures: List[Dict[str, Any]] = []

    def _collect_failures(self, max_examples: int = 3):
        """Collect failing examples (prediction vs. expected label) from the evaluator.

        These are shown to the proposer so it can see *why* the current
        workflow scores poorly (e.g. wrong output format), not just the score.
        """
        self._last_failures = []
        records_fn = getattr(self.evaluator, "get_all_evaluation_records", None)
        if records_fn is None:
            return
        for record in records_fn().values():
            metrics = record.get("metrics") or {}
            # Score each record the same way the search does, so that `objective_metric`
            # decides what counts as a failure here too. Averaging every metric
            # instead would let an unnormalized one (e.g. a length or latency)
            # push every record above the threshold and silently collect nothing.
            score = self._score(metrics) if metrics else 0.0
            if score >= 1.0:
                continue
            prediction = str(record.get("prediction"))
            if len(prediction) > 300:
                prediction = prediction[:300] + "..."
            self._last_failures.append({"prediction": prediction, "expected": record.get("label")})
            if len(self._last_failures) >= max_examples:
                break

    def _score(self, metrics: dict) -> float:
        if self.objective_metric is not None:
            if self.objective_metric not in metrics:
                raise ValueError(f"Metric '{self.objective_metric}' not found in evaluation results: {list(metrics)}")
            return float(metrics[self.objective_metric])
        values = [float(v) for v in metrics.values() if isinstance(v, (int, float))]
        if not values:
            raise ValueError(f"Evaluation returned no numeric metrics: {metrics}")
        return sum(values) / len(values)

    def evaluate(self, benchmark, eval_mode: Optional[str] = None) -> float:
        """Evaluate the program's current workflow on the benchmark."""
        graph = self.program.build_graph()
        # Explicitly sync the agent manager with the (possibly restructured)
        # graph before evaluation, so new/renamed agents are always available
        # even if the evaluator's own `update_agents` path is bypassed.
        agent_manager = getattr(self.evaluator, "agent_manager", None)
        llm = getattr(self.evaluator, "llm", None)
        if agent_manager is not None and llm is not None:
            agent_manager.update_agents_from_workflow(graph, llm.config)
        metrics = self.evaluator.evaluate(
            graph=graph,
            benchmark=benchmark,
            eval_mode=eval_mode or self.eval_mode,
            update_agents=True,
            **self.eval_config,
        )
        self._collect_failures()
        return self._score(metrics)

    def _validate(self, config: Any) -> Optional[str]:
        """Return None if the config is a valid workflow, else an error message."""
        if not isinstance(config, dict):
            return f"The proposal is not a JSON object (got {type(config).__name__})."
        try:
            graph = WorkFlowGraph.from_dict(config)
        except Exception as e:
            return f"Invalid workflow graph: {e}"
        if not graph.nodes:
            return "The workflow has no nodes."
        if not graph.find_end_nodes():
            return "The workflow has no end node."
        # Check the agent configs the way they will be checked at execution time,
        # so bad proposals fail cheaply here instead of during evaluation.
        allowed_parse_modes = {"str", "json", "xml", "title", "custom"}
        for node in graph.nodes:
            for agent in node.agents or []:
                if not isinstance(agent, dict):
                    continue
                parse_mode = agent.get("parse_mode")
                if parse_mode is not None and parse_mode not in allowed_parse_modes:
                    return (
                        f"Agent '{agent.get('name')}' has an invalid parse_mode "
                        f"{parse_mode!r}. Available choices: {sorted(allowed_parse_modes)}."
                    )
                prompt = agent.get("prompt") or ""
                template = agent.get("prompt_template")
                if not prompt and isinstance(template, dict):
                    prompt = template.get("instruction") or ""
                missing = [p.name for p in node.inputs if "{%s}" % p.name not in prompt]
                if missing:
                    return (
                        f"Agent '{agent.get('name')}' (node '{node.name}') prompt is missing "
                        f"placeholders for inputs: {missing}. Use e.g. '{{{missing[0]}}}' in the prompt."
                    )
        return None

    def _propose(self, error_feedback: Optional[str] = None) -> dict:
        """Ask the optimizer LLM for an improved workflow config."""
        if self.history:
            lines = []
            for h in self.history:
                score = f"{h['score']:.4f}" if isinstance(h.get("score"), (int, float)) else "failed"
                lines.append(f"round {h['round']}: score={score} ({h.get('note', '')})")
            history_text = "\n".join(lines)
        else:
            history_text = "(no history yet)"
        feedback = ""
        if error_feedback:
            feedback = (
                "\nYour previous proposal was invalid:\n"
                f"{error_feedback}\n"
                "Fix the error and return a corrected, complete workflow JSON.\n"
            )
        failures = ""
        if self._last_failures:
            lines = ["\nFailing examples of the current workflow (its output vs. the expected answer):"]
            for f in self._last_failures:
                lines.append(f"- workflow output: {f['prediction']!r}\n  expected: {f['expected']!r}")
            lines.append("Diagnose WHY these fail (e.g. wrong format, missing reasoning step) and fix it in your proposal.")
            failures = "\n".join(lines) + "\n"
        prompt = WORKFLOW_SEARCH_PROPOSER_PROMPT.format(
            workflow=self.program.workflow_text,
            history=history_text,
            feedback=feedback,
            failures=failures,
        )
        response = self.optimizer_llm.generate(prompt=prompt)
        return parse_json_from_llm_output(response.content)

    def optimize(self, benchmark, eval_mode: Optional[str] = None) -> dict:
        """Run the search loop. Returns ``{"best_score": float, "history": [...]}``.

        After completion, ``program.workflow_text`` holds the best workflow
        found; persist it with ``program.save(path, backup=True)``.
        """
        best_score = self.evaluate(benchmark, eval_mode)
        best_text = self.program.workflow_text
        self.history.append({"round": 0, "score": best_score, "note": "initial workflow", "config": json.loads(best_text)})
        logger.info(f"[WorkflowSearch] initial score: {best_score:.4f}")

        error_feedback = None
        for round_idx in range(1, self.max_rounds + 1):
            try:
                candidate_config = self._propose(error_feedback)
            except Exception as e:
                error_feedback = f"Failed to parse the proposal as JSON: {e}"
                logger.warning(f"[WorkflowSearch] round {round_idx}: {error_feedback}")
                self.history.append({"round": round_idx, "score": None, "note": "unparseable proposal"})
                continue

            error = self._validate(candidate_config)
            if error:
                error_feedback = error
                logger.warning(f"[WorkflowSearch] round {round_idx}: invalid proposal: {error}")
                self.history.append({"round": round_idx, "score": None, "note": f"invalid proposal: {error}"})
                continue
            error_feedback = None

            candidate_text = json.dumps(candidate_config, ensure_ascii=False, indent=2)
            self.program.workflow_text = candidate_text
            try:
                score = self.evaluate(benchmark, eval_mode)
            except Exception as e:
                self.program.workflow_text = best_text
                error_feedback = f"Evaluation failed: {e}"
                logger.warning(f"[WorkflowSearch] round {round_idx}: evaluation failed: {e}")
                self.history.append({"round": round_idx, "score": None, "note": f"evaluation failed: {e}"})
                continue

            better = score > best_score if self.maximize else score < best_score
            if better:
                best_score, best_text = score, candidate_text
                note = "new best"
            else:
                self.program.workflow_text = best_text  # hill-climb from the best
                note = "kept previous best"
            logger.info(f"[WorkflowSearch] round {round_idx}: score={score:.4f} ({note})")
            self.history.append({"round": round_idx, "score": score, "note": note, "config": candidate_config})

        self.program.workflow_text = best_text
        return {"best_score": best_score, "history": self.history}


def make_workflow_search_optimizer(
    program: WorkflowProgram,
    *,
    executor_llm,
    optimizer_llm,
    collate_func: Callable[[dict], dict],
    num_workers: int = 4,
    verbose: bool = False,
    **optimizer_kwargs,
) -> WorkflowSearchOptimizer:
    """Build a ``WorkflowSearchOptimizer`` with a pre-wired ``Evaluator``.

    Args:
        program: The :class:`WorkflowProgram` to optimize.
        executor_llm: The LLM executing the workflow.
        optimizer_llm: The LLM proposing workflow variants.
        collate_func: Maps a benchmark example to the workflow's input dict.
        num_workers: Evaluation parallelism.
        verbose: Verbose evaluation output.
        **optimizer_kwargs: Forwarded to ``WorkflowSearchOptimizer`` (e.g.
            ``max_rounds``, ``objective_metric``, ``eval_mode``).

    Returns:
        A configured ``WorkflowSearchOptimizer``.
    """
    from ..agents.agent_manager import AgentManager
    from ..evaluators import Evaluator

    agent_manager = AgentManager()
    agent_manager.add_agents_from_workflow(program.build_graph(), executor_llm.config)
    evaluator = Evaluator(
        llm=executor_llm,
        agent_manager=agent_manager,
        collate_func=collate_func,
        num_workers=num_workers,
        verbose=verbose,
    )
    return WorkflowSearchOptimizer(
        program=program,
        evaluator=evaluator,
        optimizer_llm=optimizer_llm,
        **optimizer_kwargs,
    )
