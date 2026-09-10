# Working with Skills in EvoAgentX

This tutorial walks you through the skill system in EvoAgentX. A *skill* is a reusable, file-based capability package following the `SKILL.md` convention: a directory with a `SKILL.md` file (YAML frontmatter + Markdown instructions) plus any supporting resources. We'll cover:

1. **Understanding Skills**: the `SKILL.md` format and how skills are discovered
2. **Using Skills in Agents**: expose skills to agents through the `SkillToolkit`
3. **Evolving Skills**: optimize a skill's instructions like a prompt, with the PromptRegistry, TextGrad, MIPRO or EvoPrompt
4. **Searching Workflows**: apply the same evolution loop to whole workflow definitions

By the end of this tutorial, you'll know how to create, load, share and self-improve skills in your own EvoAgentX applications.

---

## 1. Understanding Skills

A skill is a directory containing a `SKILL.md` file:

```
my_skills/
└── code_review/
    ├── SKILL.md            # required: frontmatter + instructions
    └── references/         # optional: any supporting files
        └── checklist.md
```

The `SKILL.md` file has a YAML frontmatter block with at least `name` and `description`, followed by Markdown instructions:

```markdown
---
name: code-review
description: Review source code for correctness, readability and security issues.
---

# Code Review Skill

When asked to review code, follow these steps:
1. **Understand the intent** ...
2. **Check correctness** ...
```

Skills are discovered and managed by the `SkillManager`:

```python
from evoagentx.skills import SkillManager

# Point it at a directory containing skill folders
manager = SkillManager("my_skills/")

# What skills are available?
print(manager.get_skills_overview())

# Load a skill's full instructions and bundled resources
skill = manager.load_skill("code-review")
print(skill["instructions"])
print(skill["resources"])   # e.g. ["references/checklist.md"]
```

`SkillManager` accepts three kinds of paths:

- a directory containing skill sub-directories (each with its own `SKILL.md`),
- a single skill directory, or
- a direct path to a `SKILL.md` file.

### Key Concepts

- **Progressive disclosure**: only a skill's name and description need to sit in the context; the full instructions are loaded on demand.
- **Skills are prompts**: the body of `SKILL.md` is instruction text, so it can be optimized with the same machinery used for prompts (see [Section 3](#3-evolving-skills)).
- **Portable artifacts**: a skill directory can be versioned, shared and reused across projects.

---

## 2. Using Skills in Agents

The `SkillToolkit` exposes skills to any agent that accepts tools. It provides two tools:

- `list_skills`: discover available skills (name + description)
- `load_skill`: load a skill's full instructions on demand

```python
from evoagentx.skills import SkillToolkit

toolkit = SkillToolkit(skill_paths="my_skills/")
print(toolkit.get_tool_names())   # ["list_skills", "load_skill"]
```

Pass the toolkit to an agent (requires the `tools` extra: `pip install "evoagentx[tools]"`):

```python
from evoagentx.agents import CustomizeAgent
from evoagentx.models import OpenAILLM, OpenAILLMConfig
from evoagentx.skills import SkillToolkit

agent = CustomizeAgent(
    name="ReviewAssistant",
    description="An assistant that reviews code using skills.",
    prompt="Review the following code:\n{code}",
    inputs=[
        {"name": "code", "type": "str", "description": "The code to review."}
    ],
    outputs=[
        {"name": "review", "type": "str", "description": "The review findings."}
    ],
    llm_config=OpenAILLMConfig(model="gpt-4o-mini"),
    tools=[SkillToolkit(skill_paths="my_skills/")],
)
```

The agent will call `list_skills` to see which skills exist and `load_skill` to pull the relevant instructions into its context when needed.

You can also inject a skill's instructions directly into a prompt without tools:

```python
from evoagentx.skills import SkillManager

manager = SkillManager("my_skills/")
prompt_block = manager.get_skill_prompt("code-review")
# '<skill name="code-review">\n...instructions...\n</skill>'
```

### Placeholders in skills

Skill instructions can contain `{placeholder}` variables, rendered at injection time. Unknown placeholders are left as-is:

```python
# SKILL.md body: "Review the {language} code. Keep it {style}."
manager.get_skill_prompt("code-review", language="Python", style="simple")
# '<skill name="code-review">\nReview the Python code. Keep it simple.\n</skill>'
```

### Version history

`skill.save()` overwrites the SKILL.md file. Pass `backup=True` to archive the previous version into a `.versions/` sub-directory of the skill directory first, so evolving a skill never destroys the previous version:

```python
skill.content = "evolved instructions ..."
skill.save(backup=True)   # old version archived to .versions/SKILL_<timestamp>.md
```

---

## 3. Evolving Skills

Since a skill's instructions are a prompt, EvoAgentX can **evolve** them with its self-improving optimizers. Two integration levels are available.

### 3.1 PromptRegistry integration

Register a skill's content as an optimizable field. Any optimizer built on `evoagentx.optimizers.optimizer_core` can then rewrite it:

```python
from evoagentx.optimizers.optimizer_core import PromptRegistry
from evoagentx.skills import SkillManager

manager = SkillManager("my_skills/")
registry = PromptRegistry()

# The skill's instructions become an optimizable field named "skill:code-review"
field = manager.register_skill(registry, "code-review")

# ... run your optimization loop: registry.set(field.name, candidate), evaluate, keep the best ...

# Persist the winning version back to SKILL.md (frontmatter is preserved)
manager.get_skill("code-review").save()
```

Prompts built with `manager.get_skill_prompt("code-review")` always reflect the *current* skill content, so the loop sees every rewrite immediately. See `examples/skills/skill_optimization.py` for a complete runnable loop (no API key required).

### 3.2 TextGrad integration

For gradient-free prompt optimization with TextGrad, wrap the skill as a single-node workflow whose system prompt *is* the skill's instructions:

```python
from evoagentx.models import OpenAILLM, OpenAILLMConfig
from evoagentx.skills import (
    ListBenchmark,
    SkillManager,
    make_textgrad_optimizer,
    skill_to_graph,
    update_skill_from_graph,
)

manager = SkillManager("my_skills/")
skill = manager.get_skill("code-review")

# 1. Wrap the skill as a single-node workflow
graph = skill_to_graph(
    skill,
    inputs=[{"name": "code", "type": "str", "required": True, "description": "The code to review."}],
    outputs=[{"name": "review", "type": "str", "required": True, "description": "The review findings."}],
    instruction="Review the following code:\n{code}",
)

# 2. Optimize with TextGrad (requires: pip install "evoagentx[optimizers]")
optimizer = make_textgrad_optimizer(
    graph,
    executor_llm=OpenAILLM(config=OpenAILLMConfig(model="gpt-4o-mini")),
    optimizer_llm=OpenAILLM(config=OpenAILLMConfig(model="gpt-4o")),
    collate_func=lambda x: {"code": x["code"]},
    batch_size=2,
    max_steps=3,
)

data = [
    {"code": "def transfer(a, b, amount): ...", "label": "The function is not atomic ..."},
    # more examples ...
]
optimizer.optimize(ListBenchmark("code_review_eval", data))
optimizer.restore_best_graph()

# 3. Write the evolved instructions back to the skill's SKILL.md
update_skill_from_graph(skill, optimizer.graph)
skill.save()
```

`optimize_mode="system_prompt"` (the default in `make_textgrad_optimizer`) ensures TextGrad only rewrites the skill instructions while the task instruction stays fixed. See `examples/skills/skill_textgrad_evolution.py` for the full script.

#### Choosing the metric to optimize

`ListBenchmark`'s default `evaluate` returns exact-match accuracy, but a custom `eval_func` can return any metrics you like. When it returns **more than one** metric, tell the optimizer which one decides that a skill version is better:

```python
def eval_func(prediction, label):
    return {"accuracy": ..., "chars": len(str(prediction))}   # mixed scales!

optimizer = make_textgrad_optimizer(
    ...,
    objective_metric="accuracy",   # or a callable: lambda m: m["accuracy"] - m["chars"] / 10000
)
```

Without `objective_metric`, graphs are compared by the **mean of all metric values**. That is only meaningful when every metric is on the same scale (e.g. all in `[0, 1]`): a `chars` value in the hundreds would dominate the average, so the optimizer would rank the most verbose version highest and roll back genuine improvements. The optimizer logs a warning when it sees a metric outside `[0, 1]`, but setting `objective_metric` explicitly is the reliable fix.

#### Watch out for overfitting

A skill evolved against a handful of examples will overfit to them, and TextGrad tends to *grow* prompts — in our tests a 737-character skill became 7,452 characters and had benchmark-specific wording baked into it. Evaluate on a held-out split, and diff the evolved `SKILL.md` (the previous version is kept in `.versions/` when you call `skill.save(backup=True)`) before accepting it.

### 3.3 MIPRO integration

MIPRO (via dspy) optimizes instructions and few-shot demonstrations. Wrap the skill in a `SkillProgram` — a MIPRO-compatible program whose `prompt` attribute is the skill's instructions:

```python
from evoagentx.models import OpenAILLM, OpenAILLMConfig
from evoagentx.skills import ListBenchmark, SkillManager, SkillProgram, make_mipro_optimizer

manager = SkillManager("my_skills/")
skill = manager.get_skill("code-review")

program = SkillProgram(
    skill,
    llm=OpenAILLM(config=OpenAILLMConfig(model="gpt-4o-mini")),
    instruction="Review the following code:\n{code}",   # fixed task template
    input_names=["code"],
    output_name="review",
)

optimizer = make_mipro_optimizer(
    program,
    optimizer_llm=OpenAILLM(config=OpenAILLMConfig(model="gpt-4o")),
    auto="light",
)
optimizer.optimize(ListBenchmark("code_review_eval", data))
optimizer.restore_best_program()

program.update_skill()   # copy the optimized prompt back into the skill
skill.save(backup=True)  # persist, keeping the previous version
```

### 3.4 EvoPrompt integration

EvoPrompt evolves a population of prompt candidates with genetic (GA) or differential evolution (DE) algorithms. Wrap the skill in a `SkillEvoProgram`, whose `candidates` list is the evolving population:

```python
import asyncio
from evoagentx.models import OpenAILLM, OpenAILLMConfig
from evoagentx.skills import ListBenchmark, SkillEvoProgram, SkillManager, make_evoprompt_optimizer

manager = SkillManager("my_skills/")
skill = manager.get_skill("code-review")

program = SkillEvoProgram(
    skill,
    llm=OpenAILLM(config=OpenAILLMConfig(model="gpt-4o-mini")),
)

optimizer = make_evoprompt_optimizer(
    program,
    evo_llm_config=OpenAILLMConfig(model="gpt-4o-mini"),
    algorithm="GA",           # or "DE"
    population_size=10,
    iterations=10,
)

# EvoPrompt expects examples with an "input" key and an "em" metric;
# ListBenchmark's defaults provide both.
evo_data = [{"input": "Review this code: ...", "label": "..."}]

async def run():
    await optimizer.optimize(benchmark=ListBenchmark("code_review_evo", evo_data))

asyncio.run(run())

program.update_skill()   # copy the best candidate back into the skill
skill.save(backup=True)
```

---

## 4. Searching Workflows

The same loop that evolves a skill's instructions can search the *workflow* space: a workflow's JSON config is also just a text artifact. `WorkflowProgram` holds a workflow as JSON text, and `WorkflowSearchOptimizer` evolves it — each round an LLM proposes a structural variant (adding, removing or rewiring nodes, improving prompts), invalid proposals get automatic error feedback, valid ones are evaluated on a benchmark, and the search hill-climbs on the best:

```python
from evoagentx.models import OpenAILLM, OpenAILLMConfig
from evoagentx.skills import (
    ListBenchmark, WorkflowProgram, make_workflow_search_optimizer,
)

program = WorkflowProgram(graph=my_workflow_graph)  # any WorkFlowGraph

optimizer = make_workflow_search_optimizer(
    program,
    executor_llm=OpenAILLM(config=OpenAILLMConfig(model="gpt-4o-mini")),
    optimizer_llm=OpenAILLM(config=OpenAILLMConfig(model="gpt-4o")),
    collate_func=lambda x: {"problem": x["problem"]},
    max_rounds=10,
)

result = optimizer.optimize(ListBenchmark("my_eval", data))
print(result["best_score"], result["history"])

# The best workflow is now in the program; persist it with version history:
program.save("best_workflow.json", backup=True)

# ... and execute it later:
best_graph = program.build_graph()
```

For large-scale workflow search with a curated operator set, also see the dedicated [AFlow Optimizer](aflow_optimizer.md) — `WorkflowSearchOptimizer` is the lightweight, skill-style alternative.

---

## Summary

In this tutorial, you learned how to:

- Structure reusable capabilities as `SKILL.md`-based skills and discover them with `SkillManager`
- Let agents discover and load skills on demand with `SkillToolkit`
- Use `{placeholder}` variables in skill instructions and keep version history with `save(backup=True)`
- Evolve a skill's instructions with the `PromptRegistry`, TextGrad, MIPRO or EvoPrompt, and persist the best version back to disk
- Search the workflow space by evolving workflow JSON definitions with `WorkflowSearchOptimizer`

For more details, check the examples in `examples/skills/` and the API reference.
