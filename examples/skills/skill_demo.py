"""
Demo: using SKILL.md-style skills in EvoAgentX (no LLM API key required).

Run from the repository root:

    python examples/skills/skill_demo.py
"""

import os
import json

from evoagentx.skills import SkillManager, SkillToolkit

SAMPLE_SKILLS_DIR = os.path.join(os.path.dirname(__file__), "sample_skills")


def main():
    # 1. Discover skills: point the manager at a directory containing skill folders
    manager = SkillManager(skill_paths=SAMPLE_SKILLS_DIR)

    print("=== Discovered skills ===")
    print(manager.get_skills_overview())

    # 2. Load a skill's full instructions on demand
    print("\n=== Load 'code-review' ===")
    skill = manager.load_skill("code-review")
    print(json.dumps({k: v for k, v in skill.items() if k != "instructions"}, indent=2, ensure_ascii=False))
    print("\n--- instructions ---")
    print(skill["instructions"][:300] + " ...")

    # 3. Expose skills to an agent as tools
    print("\n=== SkillToolkit ===")
    toolkit = SkillToolkit(manager=manager)
    print("tools:", toolkit.get_tool_names())

    list_tool = toolkit.get_tool("list_skills")
    print("\nlist_skills() ->")
    print(json.dumps(list_tool(), indent=2, ensure_ascii=False))

    load_tool = toolkit.get_tool("load_skill")
    result = load_tool(name="code-review")
    print("\nload_skill('code-review') resources:", result["resources"])

    # The toolkit can be passed to any agent/action that accepts tools, e.g.:
    #   agent = CustomizeAgent(..., tools=[toolkit])
    # The agent will call list_skills to discover skills and load_skill to
    # pull the relevant instructions into its context on demand.


if __name__ == "__main__":
    main()
