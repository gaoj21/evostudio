"""
Demo: evolving a SKILL.md with EvoAgentX's optimization machinery.

A skill's instructions are a prompt. This demo shows the full loop without
needing an LLM API key:

1. Register the skill's content as an optimizable field in a PromptRegistry.
2. Run tasks whose prompt is built from the *current* skill content.
3. Score the results, keep the best skill version.
4. Save the winning version back to SKILL.md.

In a real setup, step 2 calls an agent (e.g. CustomizeAgent whose prompt
includes `manager.get_skill_prompt("code-review")`) and the candidate
rewrites are produced by an optimizer (e.g. TextGrad) instead of the
hard-coded candidates used here.

Run from the repository root:

    python examples/skills/skill_optimization.py
"""

import os
import shutil
import tempfile

from evoagentx.optimizers.optimizer_core import PromptRegistry
from evoagentx.skills import SkillManager

SAMPLE_SKILLS_DIR = os.path.join(os.path.dirname(__file__), "sample_skills")

# A tiny "benchmark": reviewing this code should mention these aspects.
SAMPLE_CODE = "def transfer(a, b, amount):\n    a.balance -= amount\n    b.balance += amount\n"
REQUIRED_ASPECTS = ["correctness", "readability", "security", "regression test"]


def fake_review(prompt: str) -> dict:
    """Stand-in for an agent call: echoes which aspects the prompt asks for."""
    covered = [a for a in REQUIRED_ASPECTS if a in prompt.lower()]
    return {"review": f"covered: {covered}", "covered": covered}


def evaluate(cfg: dict, result: dict) -> float:
    return len(result["covered"]) / len(REQUIRED_ASPECTS)


def main():
    # Work on a copy so the demo never mutates the checked-in sample skills.
    workspace = tempfile.mkdtemp(prefix="eax_skill_evolution_")
    skills_dir = os.path.join(workspace, "skills")
    shutil.copytree(SAMPLE_SKILLS_DIR, skills_dir)

    manager = SkillManager(skills_dir)

    # 1. Register the skill's instructions as an optimizable prompt field.
    registry = PromptRegistry()
    field = manager.register_skill(registry, "code-review")
    print(f"registered optimizable field: {field.name}")

    # 2. Candidate skill versions. A real optimizer (TextGrad, MIPRO, ...)
    #    would generate these rewrites from failure cases via an LLM.
    original = registry.get(field.name)
    candidates = [
        original,
        original + "\n6. For every critical finding, suggest a regression test that would have caught it.\n",
    ]

    # 3. Evaluate each candidate; keep the best.
    best_cfg, best_score = None, -1.0
    for i, content in enumerate(candidates):
        registry.set(field.name, content)  # rewrites the skill in place
        # The prompt is rebuilt from the *current* skill content:
        prompt = manager.get_skill_prompt("code-review") + "\n\nCode:\n" + SAMPLE_CODE
        result = fake_review(prompt)
        score = evaluate({}, result)
        print(f"candidate {i}: score={score:.2f} ({result['review']})")
        if score > best_score:
            best_cfg, best_score = content, score

    # 4. Apply the best version and persist it back to SKILL.md.
    registry.set(field.name, best_cfg)
    saved = manager.get_skill("code-review").save()
    print(f"\nbest score: {best_score:.2f}")
    print(f"evolved skill saved to: {saved}")

    with open(saved, encoding="utf-8") as f:
        print("\n--- evolved SKILL.md (tail) ---")
        print("\n".join(f.read().splitlines()[-4:]))


if __name__ == "__main__":
    main()
