"""
Demo: evolving a SKILL.md with TextGrad (requires an LLM API key).

This wraps the sample `code-review` skill as a single-node workflow whose
system prompt is the skill's instructions, runs TextGrad optimization on a
tiny benchmark, then writes the evolved instructions back to SKILL.md.

Prerequisites:
    pip install "evoagentx[optimizers]"
    export OPENAI_API_KEY=<your-key>

Run from the repository root:

    python examples/skills/skill_textgrad_evolution.py
"""

import os
import shutil
import tempfile

from dotenv import load_dotenv

from evoagentx.models import OpenAILLM, OpenAILLMConfig
from evoagentx.skills import (
    ListBenchmark,
    SkillManager,
    make_textgrad_optimizer,
    skill_to_graph,
    update_skill_from_graph,
)

load_dotenv()

SAMPLE_SKILLS_DIR = os.path.join(os.path.dirname(__file__), "sample_skills")

# A tiny benchmark: code snippets + what a good review must point out.
DATA = [
    {
        "code": "def transfer(a, b, amount):\n    a.balance -= amount\n    b.balance += amount",
        "label": "The function is not atomic: if the process crashes between the two balance updates, money disappears. It also lacks input validation (negative amounts).",
    },
    {
        "code": "import pickle\ndef load(path):\n    return pickle.load(open(path, 'rb'))",
        "label": "pickle.load on untrusted data allows arbitrary code execution. The file handle is also never closed.",
    },
]


def collate_func(example: dict) -> dict:
    return {"code": example["code"]}


def main():
    executor_llm = OpenAILLM(config=OpenAILLMConfig(model="gpt-4o-mini"))
    optimizer_llm = OpenAILLM(config=OpenAILLMConfig(model="gpt-4o"))

    # Work on a copy so the demo never mutates the checked-in sample skills.
    workspace = tempfile.mkdtemp(prefix="eax_skill_textgrad_")
    skills_dir = os.path.join(workspace, "skills")
    shutil.copytree(SAMPLE_SKILLS_DIR, skills_dir)

    manager = SkillManager(skills_dir)
    skill = manager.get_skill("code-review")

    # 1. Wrap the skill as a single-node workflow.
    graph = skill_to_graph(
        skill,
        inputs=[{"name": "code", "type": "str", "required": True, "description": "The code to review."}],
        outputs=[{"name": "review", "type": "str", "required": True, "description": "The review findings."}],
        instruction="Review the following code:\n{code}",
    )

    # 2. Build the optimizer; "system_prompt" mode rewrites only the skill.
    optimizer = make_textgrad_optimizer(
        graph,
        executor_llm=executor_llm,
        optimizer_llm=optimizer_llm,
        collate_func=collate_func,
        batch_size=2,
        max_steps=3,
        eval_every_n_steps=1,
        rollback=True,
    )

    benchmark = ListBenchmark("code_review_eval", DATA)
    optimizer.optimize(benchmark)
    optimizer.restore_best_graph()

    # 3. Write the evolved instructions back to the skill's SKILL.md.
    update_skill_from_graph(skill, optimizer.graph)
    saved = skill.save()
    print(f"Evolved skill saved to: {saved}")


if __name__ == "__main__":
    main()
