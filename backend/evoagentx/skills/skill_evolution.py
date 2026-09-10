"""
Evolving skills with EvoAgentX's workflow optimizers.

A skill's instructions are a prompt, so they can be optimized with the same
machinery used for agent prompts. This module glues skills to the workflow
optimizers (e.g. TextGrad):

1. :func:`skill_to_graph` wraps a skill as a single-node ``WorkFlowGraph``
   whose system prompt *is* the skill's instructions.
2. The graph is optimized on a benchmark, e.g. with ``TextGradOptimizer``
   (use :func:`make_textgrad_optimizer` to build one with sensible wiring;
   keep ``optimize_mode="system_prompt"`` so only the skill is rewritten).
3. :func:`update_skill_from_graph` copies the optimized prompt back into the
   skill, and ``skill.save()`` persists it to its SKILL.md.

:class:`ListBenchmark` provides a minimal benchmark from an in-memory list of
examples, for skills that don't map to a built-in benchmark.
"""

from typing import Any, Callable, Dict, List, Optional

import json

from ..benchmark.benchmark import Benchmark
from .skill import Skill, _SafeFormatDict


class ListBenchmark(Benchmark):
    """A minimal benchmark built from an in-memory list of examples.

    Each example is a dict containing the task inputs plus a label. The same
    list is used for the train/dev/test splits, which is sufficient for
    prompt optimization loops.

    Args:
        name: The benchmark name.
        data: List of example dicts.
        label_key: The key of each example dict holding the ground-truth label.
        id_key: Optional key of each example dict holding a unique id. If not
            given, the example's index in ``data`` is used.
        eval_func: Optional ``eval_func(prediction, label) -> dict`` returning
            metrics. Defaults to exact-match accuracy on the string forms.
    """

    def __init__(
        self,
        name: str,
        data: List[dict],
        label_key: str = "label",
        id_key: Optional[str] = None,
        eval_func: Optional[Callable[[Any, Any], dict]] = None,
        **kwargs,
    ):
        self._examples = list(data)
        self.label_key = label_key
        self.id_key = id_key
        self.eval_func = eval_func
        super().__init__(name=name, path="", **kwargs)

    def _load_data(self):
        self._train_data = self._examples
        self._dev_data = self._examples
        self._test_data = self._examples

    def _get_id(self, example: Any) -> Any:
        if self.id_key is not None:
            return example[self.id_key]
        return self._examples.index(example)

    def _get_label(self, example: Any) -> Any:
        return example[self.label_key]

    def evaluate(self, prediction: Any, label: Any) -> dict:
        if self.eval_func is not None:
            return self.eval_func(prediction, label)
        match = str(prediction).strip() == str(label).strip()
        score = 1.0 if match else 0.0
        # "em" (exact match) is included because some optimizers
        # (e.g. EvoPrompt) read this metric name.
        return {"accuracy": score, "em": score}


def skill_to_graph(
    skill: Skill,
    *,
    inputs: List[dict],
    outputs: List[dict],
    instruction: str,
    goal: Optional[str] = None,
    task_name: Optional[str] = None,
    parse_mode: str = "str",
    **task_kwargs,
):
    """Wrap a skill as a single-node ``WorkFlowGraph`` for optimization.

    The skill's instructions become the system prompt of the graph's single
    agent, so optimizing the graph's system prompt (e.g. TextGrad with
    ``optimize_mode="system_prompt"``) evolves the skill itself.

    Args:
        skill: The skill to wrap.
        inputs: Task input specs, e.g. ``[{"name": "problem", "type": "str",
            "required": True, "description": "..."}]``.
        outputs: Task output specs, same format as ``inputs``.
        instruction: The task instruction prompt template, with ``{input_name}``
            placeholders for each input. This stays fixed; the skill (system
            prompt) is what gets optimized.
        goal: The workflow goal. Defaults to a text derived from the skill.
        task_name: The node/task name. Defaults to ``<skill_name>_task``.
        parse_mode: Output parse mode of the agent (default "str").
        **task_kwargs: Extra keys forwarded to the task dict (e.g. ``tool_names``).

    Returns:
        A ``SequentialWorkFlowGraph`` with a single node.
    """
    from ..prompts import StringTemplate
    from ..workflow.workflow_graph import SequentialWorkFlowGraph

    safe_name = skill.name.replace("-", "_").replace(" ", "_")
    description = skill.description or f"Apply the '{skill.name}' skill."
    task = {
        "name": task_name or f"{safe_name}_task",
        "description": description,
        "inputs": inputs,
        "outputs": outputs,
        "system_prompt": skill.content,
        "prompt_template": StringTemplate(instruction=instruction),
        "parse_mode": parse_mode,
        **task_kwargs,
    }
    return SequentialWorkFlowGraph(goal=goal or description, tasks=[task])


def update_skill_from_graph(skill: Skill, graph) -> str:
    """Copy the (possibly optimized) system prompt of a skill graph back into the skill.

    Call this after optimizing the graph built by :func:`skill_to_graph`, then
    ``skill.save()`` to persist the evolved instructions to the SKILL.md file.

    Args:
        skill: The skill to update.
        graph: The optimized graph (single-node, as built by :func:`skill_to_graph`).

    Returns:
        The new skill content.
    """
    agent = graph.nodes[0].agents[0]
    content = agent.get("system_prompt") if isinstance(agent, dict) else agent.system_prompt
    if not content:
        raise ValueError("The graph's agent has no system prompt to write back to the skill.")
    skill.content = content
    return content


def make_textgrad_optimizer(
    graph,
    *,
    executor_llm,
    optimizer_llm,
    collate_func: Callable[[dict], dict],
    optimize_mode: str = "system_prompt",
    num_workers: int = 4,
    verbose: bool = False,
    **optimizer_kwargs,
):
    """Build a ``TextGradOptimizer`` pre-wired for skill evolution.

    Args:
        graph: A graph built by :func:`skill_to_graph`.
        executor_llm: The LLM executing the workflow.
        optimizer_llm: The LLM computing textual gradients and rewriting prompts.
        collate_func: Maps a benchmark example to the graph's input dict.
        optimize_mode: Keep the default ``"system_prompt"`` so TextGrad only
            rewrites the skill instructions (the task instruction stays fixed).
        num_workers: Evaluation parallelism.
        verbose: Verbose evaluation output.
        **optimizer_kwargs: Forwarded to ``TextGradOptimizer`` (e.g.
            ``batch_size``, ``max_steps``, ``eval_every_n_steps``, ``rollback``).

    Returns:
        A configured ``TextGradOptimizer``. After ``optimizer.optimize(dataset)``,
        use ``update_skill_from_graph(skill, optimizer.graph)`` and ``skill.save()``.
    """
    from ..agents.agent_manager import AgentManager
    from ..evaluators import Evaluator
    from ..optimizers import TextGradOptimizer

    agent_manager = AgentManager()
    agent_manager.add_agents_from_workflow(graph, executor_llm.config)
    evaluator = Evaluator(
        llm=executor_llm,
        agent_manager=agent_manager,
        collate_func=collate_func,
        num_workers=num_workers,
        verbose=verbose,
    )
    return TextGradOptimizer(
        graph=graph,
        optimize_mode=optimize_mode,
        executor_llm=executor_llm,
        optimizer_llm=optimizer_llm,
        evaluator=evaluator,
        **optimizer_kwargs,
    )


class SkillProgram:
    """A MIPRO-compatible program that wraps a skill.

    The program's ``prompt`` attribute (initialized from the skill's
    instructions) is what MIPRO rewrites. The task instruction (with
    ``{placeholder}`` variables) stays fixed and is appended to the skill
    instructions at execution time.

    After optimization, call :meth:`update_skill` to copy the optimized prompt
    back into the skill, then ``skill.save()`` to persist it.

    Args:
        skill: The skill to optimize.
        llm: The LLM used to execute the program (a ``BaseLLM`` instance).
        instruction: Fixed task instruction template with ``{placeholder}``
            variables matching ``input_names``.
        input_names: Names of the inputs the program accepts (used for both
            template formatting and the optimizer registry).
        output_name: Name under which the program's output is recorded in the
            execution data.
    """

    def __init__(
        self,
        skill: Skill,
        llm,
        instruction: str,
        input_names: List[str],
        output_name: str = "output",
    ):
        self.skill = skill
        self.llm = llm
        self.instruction = instruction
        self.input_names = list(input_names)
        self.output_name = output_name
        self.prompt = skill.content

    def save(self, path: str):
        """Save the program state (the current prompt) to a JSON file."""
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"prompt": self.prompt}, f, ensure_ascii=False, indent=2)

    def load(self, path: str):
        """Load the program state from a JSON file."""
        with open(path, "r", encoding="utf-8") as f:
            self.prompt = json.load(f)["prompt"]

    def __call__(self, **kwargs):
        """Execute the program on one example.

        Returns a tuple of (prediction, execution_data), as required by
        ``MiproOptimizer``.
        """
        task_inputs = {k: v for k, v in kwargs.items() if k in self.input_names}
        full_prompt = self.prompt + "\n\n" + self.instruction.format_map(_SafeFormatDict(task_inputs))
        response = self.llm.generate(prompt=full_prompt)
        output = response.content
        return output, {**task_inputs, self.output_name: output}

    def update_skill(self) -> str:
        """Copy the (optimized) prompt back into the skill and return it."""
        self.skill.content = self.prompt
        return self.skill.content


class SkillEvoProgram:
    """An EvoPrompt-compatible program that wraps a skill.

    ``candidates`` holds the population of skill versions that GA/DE
    optimizers evolve; it starts as ``[skill.content]`` (plus any extra
    seed candidates). During optimization the optimizers replace this
    attribute with new generations; afterwards it holds the best version
    (as a string or a single-element list).

    After optimization, call :meth:`update_skill` to copy the best candidate
    back into the skill, then ``skill.save()`` to persist it.

    Args:
        skill: The skill to optimize.
        llm: The LLM used to execute the program (a ``BaseLLM`` instance).
        candidates: Optional extra seed skill versions for the initial
            population. The skill's current instructions are always included
            as the first candidate.
        prompt_template: Template used to build the full prompt from the
            current candidate and the task input. Must contain ``{skill}``
            and ``{input}`` placeholders.
    """

    def __init__(
        self,
        skill: Skill,
        llm,
        candidates: Optional[List[str]] = None,
        prompt_template: str = "{skill}\n\n{input}",
    ):
        self.skill = skill
        self.llm = llm
        self.prompt_template = prompt_template
        self.candidates = [skill.content] + list(candidates or [])

    def _current_candidate(self) -> str:
        if isinstance(self.candidates, list):
            return self.candidates[0]
        return self.candidates

    def save(self, path: str):
        """Save the program state (the current candidates) to a JSON file."""
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"candidates": self.candidates}, f, ensure_ascii=False, indent=2)

    def load(self, path: str):
        """Load the program state from a JSON file."""
        with open(path, "r", encoding="utf-8") as f:
            self.candidates = json.load(f)["candidates"]

    def __call__(self, input: str):
        """Execute the program on one example, as required by EvoPrompt.

        Returns a tuple of (prediction, metadata).
        """
        full_prompt = self.prompt_template.format(skill=self._current_candidate(), input=input)
        response = self.llm.generate(prompt=full_prompt)
        return response.content.strip(), {"full_prompt": full_prompt}

    def update_skill(self) -> str:
        """Copy the best (optimized) candidate back into the skill and return it."""
        self.skill.content = self._current_candidate()
        return self.skill.content


def make_mipro_optimizer(
    program: SkillProgram,
    *,
    optimizer_llm,
    input_names: Optional[List[str]] = None,
    output_names: Optional[List[str]] = None,
    **optimizer_kwargs,
):
    """Build a ``MiproOptimizer`` pre-wired for skill evolution.

    Args:
        program: A :class:`SkillProgram` wrapping the skill to optimize.
        optimizer_llm: The LLM used by MIPRO to propose better instructions.
        input_names: Input keys recorded for the tracked prompt. Defaults to
            the program's ``input_names``.
        output_names: Output keys recorded for the tracked prompt. Defaults to
            the program's ``output_name``.
        **optimizer_kwargs: Forwarded to ``MiproOptimizer`` (e.g. ``auto``,
            ``max_bootstrapped_demos``, ``num_threads``, ``save_path``).

    Returns:
        A configured ``MiproOptimizer``. After ``optimizer.optimize(dataset)``
        and ``optimizer.restore_best_program()``, use ``program.update_skill()``
        and ``program.skill.save()`` to persist the evolved skill.
    """
    from ..optimizers import MiproOptimizer
    from ..utils.mipro_utils.register_utils import MiproRegistry

    registry = MiproRegistry()
    registry.track(
        program,
        "prompt",
        input_names=input_names or program.input_names,
        output_names=output_names or [program.output_name],
    )
    return MiproOptimizer(
        registry=registry,
        program=program,
        optimizer_llm=optimizer_llm,
        **optimizer_kwargs,
    )


def make_evoprompt_optimizer(
    program: SkillEvoProgram,
    *,
    evo_llm_config,
    algorithm: str = "GA",
    population_size: int = 10,
    iterations: int = 10,
    field_name: str = "candidates",
    **optimizer_kwargs,
):
    """Build an EvoPrompt optimizer (GA or DE) pre-wired for skill evolution.

    Args:
        program: A :class:`SkillEvoProgram` wrapping the skill to optimize.
        evo_llm_config: LLM config used by the optimizer to generate new
            candidate skill versions (e.g. an ``OpenAILLMConfig``).
        algorithm: ``"GA"`` (genetic algorithm) or ``"DE"`` (differential
            evolution).
        population_size: Size of the evolution population.
        iterations: Number of evolution iterations.
        field_name: The program attribute holding the candidate population.
        **optimizer_kwargs: Forwarded to the optimizer (e.g.
            ``concurrency_limit``, ``enable_logging``, ``early_stopping_patience``).

    Returns:
        A configured ``GAOptimizer`` or ``DEOptimizer``. After
        ``await optimizer.optimize(benchmark)``, use ``program.update_skill()``
        and ``program.skill.save()`` to persist the evolved skill. Note that
        EvoPrompt expects benchmark examples with an ``"input"`` key and an
        ``"em"`` metric (both provided by :class:`ListBenchmark` defaults).
    """
    from ..optimizers.engine.registry import ParamRegistry
    from ..optimizers.evoprompt_optimizer import DEOptimizer, GAOptimizer

    optimizers = {"GA": GAOptimizer, "DE": DEOptimizer}
    if algorithm.upper() not in optimizers:
        raise ValueError(f"Unsupported algorithm '{algorithm}'. Choices: {list(optimizers)}")

    registry = ParamRegistry()
    registry.track(program, field_name, name="skill_versions")
    return optimizers[algorithm.upper()](
        registry=registry,
        program=program,
        population_size=population_size,
        iterations=iterations,
        llm_config=evo_llm_config,
        **optimizer_kwargs,
    )
