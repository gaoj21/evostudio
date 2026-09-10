# 在 EvoAgentX 中使用 Skill

本教程介绍 EvoAgentX 的 skill（技能）系统。skill 是一种可复用的、基于文件的能力包，遵循 `SKILL.md` 约定：一个包含 `SKILL.md` 文件（YAML frontmatter + Markdown 指令）的目录，外加任意附属资源文件。本教程涵盖：

1. **理解 Skill**:`SKILL.md` 的格式与 skill 的发现机制
2. **在 Agent 中使用 Skill**：通过 `SkillToolkit` 把 skill 暴露给 agent
3. **进化 Skill**：像优化 prompt 一样优化 skill 的指令（PromptRegistry、TextGrad、MIPRO 或 EvoPrompt)
4. **搜索工作流**：把同一套进化循环应用到整个 workflow 定义上

学完本教程，你将能够在自己的 EvoAgentX 应用中创建、加载、共享并自我改进 skill。

---

## 1. 理解 Skill

skill 就是一个包含 `SKILL.md` 文件的目录：

```
my_skills/
└── code_review/
    ├── SKILL.md            # 必需:frontmatter + 指令
    └── references/         # 可选:任意附属文件
        └── checklist.md
```

`SKILL.md` 文件由 YAML frontmatter（至少包含 `name` 和 `description`）和 Markdown 指令正文组成：

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

skill 由 `SkillManager` 发现和管理：

```python
from evoagentx.skills import SkillManager

# 指向包含 skill 目录的根目录
manager = SkillManager("my_skills/")

# 查看可用的 skill
print(manager.get_skills_overview())

# 加载某个 skill 的完整指令与附属资源
skill = manager.load_skill("code-review")
print(skill["instructions"])
print(skill["resources"])   # 例如 ["references/checklist.md"]
```

`SkillManager` 接受三种路径形式：

- 包含多个 skill 子目录的根目录（每个子目录有自己的 `SKILL.md`);
- 单个 skill 目录；
- 直接指向某个 `SKILL.md` 文件的路径。

### 核心概念

- **渐进披露（progressive disclosure)**：上下文中平时只需放 skill 的名称和描述，完整指令按需加载。
- **Skill 即 prompt**:`SKILL.md` 的正文就是指令文本，因此可以用优化 prompt 的同一套机制来优化它（见[第 3 节](#3-进化-skill))。
- **可移植的制品**:skill 目录可以版本化、共享、跨项目复用。

---

## 2. 在 Agent 中使用 Skill

`SkillToolkit` 可以把 skill 暴露给任何支持工具的 agent，它提供两个工具：

- `list_skills`：发现可用的 skill（名称 + 描述）
- `load_skill`：按需加载某个 skill 的完整指令

```python
from evoagentx.skills import SkillToolkit

toolkit = SkillToolkit(skill_paths="my_skills/")
print(toolkit.get_tool_names())   # ["list_skills", "load_skill"]
```

把 toolkit 传给 agent（需要 `tools` 扩展：`pip install "evoagentx[tools]"`):

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

agent 会先调用 `list_skills` 查看有哪些 skill，需要时再调用 `load_skill` 把相关指令拉进上下文。

也可以不使用工具，直接把 skill 指令注入 prompt:

```python
from evoagentx.skills import SkillManager

manager = SkillManager("my_skills/")
prompt_block = manager.get_skill_prompt("code-review")
# '<skill name="code-review">\n...指令...\n</skill>'
```

### skill 中的占位符

skill 指令可以包含 `{placeholder}` 变量，在注入时渲染；未提供值的占位符会原样保留：

```python
# SKILL.md 正文:"Review the {language} code. Keep it {style}."
manager.get_skill_prompt("code-review", language="Python", style="simple")
# '<skill name="code-review">\nReview the Python code. Keep it simple.\n</skill>'
```

### 版本历史

`skill.save()` 会覆盖 SKILL.md 文件。传入 `backup=True` 会先把旧版本归档到 skill 目录下的 `.versions/` 子目录，进化 skill 时不会丢失历史版本：

```python
skill.content = "evolved instructions ..."
skill.save(backup=True)   # 旧版本归档到 .versions/SKILL_<时间戳>.md
```

---

## 3. 进化 Skill

由于 skill 的指令本质上是 prompt,EvoAgentX 可以用其自进化优化器来**进化**它。提供两个集成层级。

### 3.1 PromptRegistry 集成

把 skill 的正文注册为可优化字段，任何基于 `evoagentx.optimizers.optimizer_core` 的优化器都可以改写它：

```python
from evoagentx.optimizers.optimizer_core import PromptRegistry
from evoagentx.skills import SkillManager

manager = SkillManager("my_skills/")
registry = PromptRegistry()

# skill 的指令被注册为名为 "skill:code-review" 的可优化字段
field = manager.register_skill(registry, "code-review")

# ... 运行优化循环:registry.set(field.name, 候选版本) → 评估 → 保留最优 ...

# 把最优版本写回 SKILL.md(frontmatter 会被保留)
manager.get_skill("code-review").save()
```

`manager.get_skill_prompt("code-review")` 生成的 prompt 始终反映 skill 的**当前**内容，因此优化循环能立即看到每次改写。完整可运行的示例（无需 API key）见 `examples/skills/skill_optimization.py`。

### 3.2 TextGrad 集成

要用 TextGrad 做无梯度的 prompt 优化，可以把 skill 包装成一个单节点工作流，其 system prompt 就是 skill 的指令：

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

# 1. 把 skill 包装成单节点工作流
graph = skill_to_graph(
    skill,
    inputs=[{"name": "code", "type": "str", "required": True, "description": "The code to review."}],
    outputs=[{"name": "review", "type": "str", "required": True, "description": "The review findings."}],
    instruction="Review the following code:\n{code}",
)

# 2. 用 TextGrad 优化(需要:pip install "evoagentx[optimizers]")
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
    # 更多样本 ...
]
optimizer.optimize(ListBenchmark("code_review_eval", data))
optimizer.restore_best_graph()

# 3. 把进化后的指令写回 skill 的 SKILL.md
update_skill_from_graph(skill, optimizer.graph)
skill.save()
```

`make_textgrad_optimizer` 默认 `optimize_mode="system_prompt"`，确保 TextGrad 只改写 skill 指令，任务指令保持不变。完整脚本见 `examples/skills/skill_textgrad_evolution.py`。

#### 指定优化目标指标

`ListBenchmark` 默认的 `evaluate` 返回精确匹配准确率，但自定义 `eval_func` 可以返回任意指标。当它返回**多个**指标时，必须告诉优化器以哪一个作为「更好」的判据：

```python
def eval_func(prediction, label):
    return {"accuracy": ..., "chars": len(str(prediction))}   # 量级不一致！

optimizer = make_textgrad_optimizer(
    ...,
    objective_metric="accuracy",   # 也可传可调用对象：lambda m: m["accuracy"] - m["chars"] / 10000
)
```

不指定 `objective_metric` 时，优化器按**所有指标值的均值**比较不同版本。这只在所有指标量级相同（例如都在 `[0, 1]`）时才有意义：一个数百量级的 `chars` 会主导均值，导致优化器把最啰嗦的版本判为最优，并回滚掉真正的改进。优化器在发现超出 `[0, 1]` 的指标时会打印警告，但显式设置 `objective_metric` 才是可靠做法。

#### 注意过拟合

用少量样本进化出的 skill 会过拟合到这些样本上，而且 TextGrad 倾向于让提示**膨胀** —— 我们的实测中，737 字符的 skill 变成了 7452 字符，并且把基准测试特有的措辞写死了进去。请在留出集上评估，并在接受之前 diff 进化后的 `SKILL.md`（调用 `skill.save(backup=True)` 时旧版本会保留在 `.versions/` 中）。

### 3.3 MIPRO 集成

MIPRO（基于 dspy）优化指令和 few-shot 示例。用 `SkillProgram` 包装 skill——它是一个 MIPRO 兼容的 program，其 `prompt` 属性就是 skill 的指令：

```python
from evoagentx.models import OpenAILLM, OpenAILLMConfig
from evoagentx.skills import ListBenchmark, SkillManager, SkillProgram, make_mipro_optimizer

manager = SkillManager("my_skills/")
skill = manager.get_skill("code-review")

program = SkillProgram(
    skill,
    llm=OpenAILLM(config=OpenAILLMConfig(model="gpt-4o-mini")),
    instruction="Review the following code:\n{code}",   # 固定的任务模板
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

program.update_skill()   # 把优化后的 prompt 写回 skill
skill.save(backup=True)  # 落盘,并保留旧版本
```

### 3.4 EvoPrompt 集成

EvoPrompt 用遗传算法（GA）或差分进化（DE）进化一组 prompt 候选。用 `SkillEvoProgram` 包装 skill，其 `candidates` 列表就是被进化的种群：

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
    algorithm="GA",           # 或 "DE"
    population_size=10,
    iterations=10,
)

# EvoPrompt 要求样本包含 "input" 键、评估指标包含 "em";
# ListBenchmark 的默认行为正好满足这两点。
evo_data = [{"input": "Review this code: ...", "label": "..."}]

async def run():
    await optimizer.optimize(benchmark=ListBenchmark("code_review_evo", evo_data))

asyncio.run(run())

program.update_skill()   # 把最优候选写回 skill
skill.save(backup=True)
```

---

## 4. 搜索工作流（Workflow Search)

进化 skill 指令的同一套循环也可以用来搜索**工作流空间**：workflow 的 JSON 配置同样是一段文本制品。`WorkflowProgram` 把 workflow 保存为 JSON 文本，`WorkflowSearchOptimizer` 负责进化它——每轮由 LLM 提出结构变体（增删节点、改连线、改进 prompt)，非法提案会自动获得错误反馈并自我修正，合法变体在 benchmark 上评估，搜索过程沿最优解爬山：

```python
from evoagentx.models import OpenAILLM, OpenAILLMConfig
from evoagentx.skills import (
    ListBenchmark, WorkflowProgram, make_workflow_search_optimizer,
)

program = WorkflowProgram(graph=my_workflow_graph)  # 任意 WorkFlowGraph

optimizer = make_workflow_search_optimizer(
    program,
    executor_llm=OpenAILLM(config=OpenAILLMConfig(model="gpt-4o-mini")),
    optimizer_llm=OpenAILLM(config=OpenAILLMConfig(model="gpt-4o")),
    collate_func=lambda x: {"problem": x["problem"]},
    max_rounds=10,
)

result = optimizer.optimize(ListBenchmark("my_eval", data))
print(result["best_score"], result["history"])

# 最优 workflow 已在 program 中,带版本历史地落盘:
program.save("best_workflow.json", backup=True)

# 之后可随时重建执行:
best_graph = program.build_graph()
```

如果需要基于精选算子集的大规模 workflow 搜索，也可以参阅专门的 [AFlow 优化器](aflow_optimizer.md)——`WorkflowSearchOptimizer` 是轻量的、skill 风格的替代方案。

---

## 总结

通过本教程，你学会了：

- 用 `SKILL.md` 格式组织可复用能力，并用 `SkillManager` 发现和管理它们
- 用 `SkillToolkit` 让 agent 按需发现和加载 skill
- 在 skill 指令中使用 `{placeholder}` 变量，并用 `save(backup=True)` 保留版本历史
- 用 `PromptRegistry`、TextGrad、MIPRO 或 EvoPrompt 进化 skill 的指令，并把最优版本写回磁盘
- 用 `WorkflowSearchOptimizer` 进化 workflow 的 JSON 定义，搜索工作流空间

更多细节请查看 `examples/skills/` 下的示例和 API 参考文档。
