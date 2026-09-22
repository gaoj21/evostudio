from .skill import Skill, SkillManager, parse_skill_file, SKILL_FILE_NAME

__all__ = [
    "Skill",
    "SkillManager",
    "parse_skill_file",
    "SKILL_FILE_NAME",
    "SkillToolkit",
    "ListSkillsTool",
    "LoadSkillTool",
    "ListBenchmark",
    "skill_to_graph",
    "update_skill_from_graph",
    "make_textgrad_optimizer",
    "SkillProgram",
    "SkillEvoProgram",
    "make_mipro_optimizer",
    "make_evoprompt_optimizer",
    "WorkflowProgram",
    "WorkflowSearchOptimizer",
    "make_workflow_search_optimizer",
]


def __getattr__(name):
    # Lazily expose the toolkit and evolution helpers so that `evoagentx.skills`
    # can be used (SkillManager, parsing, etc.) without the optional `tools`
    # and `optimizers` dependencies.
    if name in ("SkillToolkit", "ListSkillsTool", "LoadSkillTool"):
        from ..tools import skill_tool
        return getattr(skill_tool, name)
    if name in (
        "ListBenchmark",
        "skill_to_graph",
        "update_skill_from_graph",
        "make_textgrad_optimizer",
        "SkillProgram",
        "SkillEvoProgram",
        "make_mipro_optimizer",
        "make_evoprompt_optimizer",
    ):
        from . import skill_evolution
        return getattr(skill_evolution, name)
    if name in ("WorkflowProgram", "WorkflowSearchOptimizer", "make_workflow_search_optimizer"):
        from . import workflow_search
        return getattr(workflow_search, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
