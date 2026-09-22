"""
Expose skills to agents as tools.

``SkillToolkit`` wraps a :class:`~evoagentx.skills.skill.SkillManager` and
provides two tools that follow the progressive-disclosure pattern of
Agent Skills:

- ``list_skills``: discover which skills are available (name + description).
- ``load_skill``: load the full instructions (and bundled resources) of a
  skill on demand, so only the needed skill enters the context.

Example::

    from evoagentx.tools import SkillToolkit

    toolkit = SkillToolkit(skill_paths="path/to/my_skills")
    # pass `toolkit` to any agent/action that accepts tools
"""

from typing import Any, Dict, List, Optional, Union

from .tool import Tool, Toolkit
from ..skills.skill import SkillManager


class ListSkillsTool(Tool):
    name: str = "list_skills"
    description: str = (
        "List all available skills. Returns each skill's name, description and path. "
        "Call this first to discover relevant skills, then use 'load_skill' to get "
        "the full instructions of the skill you need."
    )
    inputs: Dict[str, Dict[str, Any]] = {}
    required: Optional[List[str]] = None
    manager: Any = None

    def __call__(self) -> dict:
        return {"skills": self.manager.list_skills()}


class LoadSkillTool(Tool):
    name: str = "load_skill"
    description: str = (
        "Load the full instructions of a skill by name. Returns the skill's Markdown "
        "instructions and a list of bundled resource files (relative to the skill "
        "directory, whose absolute path is also returned)."
    )
    inputs: Dict[str, Dict[str, Any]] = {
        "name": {
            "type": "string",
            "description": "The name of the skill to load, as returned by 'list_skills'.",
        }
    }
    required: List[str] = ["name"]
    manager: Any = None

    def __call__(self, name: str) -> dict:
        try:
            return self.manager.load_skill(name)
        except ValueError as e:
            return {"error": str(e)}


class SkillToolkit(Toolkit):
    """A toolkit that lets agents discover and load SKILL.md-based skills.

    Args:
        skill_paths: A path or list of paths to scan for skills (ignored if
            ``manager`` is given). See :class:`SkillManager` for accepted forms.
        manager: An existing :class:`SkillManager` instance to expose.
        name: The toolkit name.
    """

    def __init__(
        self,
        skill_paths: Optional[Union[str, List[str]]] = None,
        manager: Optional[SkillManager] = None,
        name: str = "skill_toolkit",
    ):
        if manager is None:
            manager = SkillManager(skill_paths)
        tools = [ListSkillsTool(manager=manager), LoadSkillTool(manager=manager)]
        super().__init__(name=name, tools=tools, manager=manager)
