"""
Skill support for EvoAgentX.

A "skill" is a self-contained directory with a ``SKILL.md`` file at its root,
following the Agent Skills convention (https://agentskills.io). The ``SKILL.md``
file contains a YAML frontmatter block with at least ``name`` and
``description``, followed by Markdown instructions::

    my-skill/
    ├── SKILL.md          # required: frontmatter + instructions
    ├── scripts/          # optional: any supporting files
    └── references/       # optional

Example ``SKILL.md``::

    ---
    name: my-skill
    description: What the skill does and when to use it.
    ---

    # My Skill

    Step-by-step instructions for the agent ...

Skills are discovered by :class:`SkillManager` and can be exposed to agents
through ``evoagentx.skills.SkillToolkit``.
"""

import os
import shutil
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

import yaml

from ..core.logging import logger
from ..core.module import BaseModule

SKILL_FILE_NAME = "SKILL.md"


class _SafeFormatDict(dict):
    """Format mapping that leaves unknown ``{placeholders}`` untouched."""

    def __missing__(self, key):
        return "{" + key + "}"


class Skill(BaseModule):
    """A single skill parsed from a ``SKILL.md`` file."""

    name: str
    description: str
    path: str
    """Absolute path of the skill directory (the directory containing SKILL.md)."""
    content: str
    """The Markdown body of SKILL.md (the instructions, without frontmatter)."""
    metadata: Dict[str, Any] = {}
    """Any extra frontmatter fields beyond name/description."""

    @property
    def skill_file(self) -> str:
        return os.path.join(self.path, SKILL_FILE_NAME)

    def get_resources(self) -> List[str]:
        """List supporting files bundled with the skill (relative to the skill directory).

        ``SKILL.md`` itself and hidden files are excluded.
        """
        resources = []
        for root, dirs, files in os.walk(self.path):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for file_name in files:
                if file_name == SKILL_FILE_NAME or file_name.startswith("."):
                    continue
                rel_path = os.path.relpath(os.path.join(root, file_name), self.path)
                resources.append(rel_path)
        return sorted(resources)

    def render(self, **kwargs) -> str:
        """Return the skill's instructions with ``{placeholder}`` values filled in.

        Placeholders without a matching keyword argument are left as-is, so
        instructions containing literal braces survive rendering.

        Args:
            **kwargs: Values for the ``{placeholder}`` variables in the content.

        Returns:
            The rendered instructions.
        """
        if not kwargs:
            return self.content
        return self.content.format_map(_SafeFormatDict(kwargs))

    def save(self, path: Optional[str] = None, backup: bool = False) -> str:
        """Write the skill back to a ``SKILL.md`` file (frontmatter + content).

        Args:
            path: Target SKILL.md path. Defaults to the skill's own SKILL.md.
            backup: If True, archive the existing SKILL.md into a ``.versions``
                sub-directory of the skill directory before overwriting it, so
                evolved versions never destroy the previous one.

        Returns:
            The path that was written.
        """
        file_path = path or self.skill_file
        if backup and os.path.exists(file_path):
            versions_dir = os.path.join(self.path, ".versions")
            os.makedirs(versions_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = os.path.join(versions_dir, f"SKILL_{timestamp}.md")
            shutil.copy2(file_path, backup_path)
            logger.info(f"Backed up previous skill version to '{backup_path}'.")

        frontmatter = {"name": self.name, "description": self.description, **self.metadata}
        text = (
            "---\n"
            + yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
            + "---\n\n"
            + self.content.strip()
            + "\n"
        )
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(text)
        return file_path

    def as_optimizable_field(self, name: Optional[str] = None):
        """Expose the skill's instructions as an optimizable prompt field.

        Returns an :class:`~evoagentx.optimizers.optimizer_core.OptimizableField`
        whose getter/setter read and write this skill's ``content``. Register it
        in a ``PromptRegistry`` so optimizers can rewrite the skill like any
        other prompt, then call :meth:`save` to persist the best version.

        Args:
            name: Field name in the registry. Defaults to ``skill:<skill-name>``.
        """
        from ..optimizers.optimizer_core import OptimizableField

        return OptimizableField(
            name=name or f"skill:{self.name}",
            getter=lambda: self.content,
            setter=lambda value: setattr(self, "content", str(value)),
        )


def _split_frontmatter(text: str) -> tuple:
    """Split a SKILL.md text into (frontmatter dict, markdown body)."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                frontmatter = yaml.safe_load("\n".join(lines[1:i])) or {}
                body = "\n".join(lines[i + 1:]).strip()
                return frontmatter, body
    return {}, text.strip()


def parse_skill_file(skill_file: str) -> Skill:
    """Parse a ``SKILL.md`` file into a :class:`Skill`.

    ``name`` and ``description`` are taken from the YAML frontmatter. If the
    frontmatter (or the fields) is missing, the name falls back to the skill
    directory name and the description falls back to the first non-empty line
    of the body.
    """
    with open(skill_file, "r", encoding="utf-8") as f:
        text = f.read()

    frontmatter, body = _split_frontmatter(text)
    if not isinstance(frontmatter, dict):
        raise ValueError(f"Invalid frontmatter in '{skill_file}': expected a YAML mapping")

    skill_dir = os.path.dirname(os.path.abspath(skill_file))
    name = frontmatter.get("name") or os.path.basename(skill_dir)
    description = frontmatter.get("description") or ""
    if not description:
        for line in body.splitlines():
            line = line.strip().lstrip("#").strip()
            if line:
                description = line
                break

    metadata = {k: v for k, v in frontmatter.items() if k not in ("name", "description")}
    return Skill(
        name=str(name),
        description=str(description),
        path=skill_dir,
        content=body,
        metadata=metadata,
    )


class SkillManager:
    """Discovers, registers and loads skills from one or more locations.

    Each path passed to the manager can be:

    - a directory containing skill sub-directories (each with its own ``SKILL.md``),
    - a single skill directory (containing ``SKILL.md``), or
    - a direct path to a ``SKILL.md`` file.

    Args:
        skill_paths: A path or a list of paths to scan for skills.
    """

    def __init__(self, skill_paths: Optional[Union[str, List[str]]] = None):
        self.skills: Dict[str, Skill] = {}
        if skill_paths:
            self.add_paths(skill_paths)

    def add_paths(self, skill_paths: Union[str, List[str]]):
        """Scan additional path(s) and register the skills found there."""
        if isinstance(skill_paths, str):
            skill_paths = [skill_paths]
        for path in skill_paths:
            path = os.path.abspath(os.path.expanduser(path))
            if os.path.isfile(path):
                self._register(parse_skill_file(path))
            elif os.path.isdir(path):
                self._scan_directory(path)
            else:
                logger.warning(f"Skill path '{path}' does not exist, skipped.")

    def _scan_directory(self, root: str):
        # The directory itself is a skill
        if os.path.isfile(os.path.join(root, SKILL_FILE_NAME)):
            self._register(parse_skill_file(os.path.join(root, SKILL_FILE_NAME)))
            return
        for entry in sorted(os.listdir(root)):
            sub_dir = os.path.join(root, entry)
            skill_file = os.path.join(sub_dir, SKILL_FILE_NAME)
            if os.path.isdir(sub_dir) and os.path.isfile(skill_file):
                try:
                    self._register(parse_skill_file(skill_file))
                except Exception as e:
                    logger.warning(f"Failed to parse skill file '{skill_file}': {e}")

    def _register(self, skill: Skill):
        if skill.name in self.skills:
            logger.warning(
                f"Duplicate skill name '{skill.name}' at '{skill.path}', "
                f"overriding the one at '{self.skills[skill.name].path}'."
            )
        self.skills[skill.name] = skill

    def list_skills(self) -> List[Dict[str, str]]:
        """Return a summary (name, description, path) of all registered skills."""
        return [
            {"name": skill.name, "description": skill.description, "path": skill.path}
            for skill in self.skills.values()
        ]

    def get_skill(self, name: str) -> Skill:
        """Get a registered skill by name."""
        if name not in self.skills:
            available = ", ".join(sorted(self.skills)) or "(none)"
            raise ValueError(f"Skill '{name}' not found. Available skills: {available}")
        return self.skills[name]

    def load_skill(self, name: str) -> Dict[str, Any]:
        """Load the full content of a skill: instructions plus bundled resources."""
        skill = self.get_skill(name)
        return {
            "name": skill.name,
            "description": skill.description,
            "path": skill.path,
            "instructions": skill.content,
            "resources": skill.get_resources(),
        }

    def get_skills_overview(self) -> str:
        """Render a text overview of available skills, suitable for inclusion in prompts."""
        if not self.skills:
            return "No skills available."
        lines = ["Available skills:"]
        for skill in self.skills.values():
            lines.append(f"- {skill.name}: {skill.description}")
        return "\n".join(lines)

    def get_skill_prompt(self, name: str, **kwargs) -> str:
        """Render a skill's instructions as a prompt block for injection into agent prompts.

        The block is built from the skill's *current* content, so when an
        optimizer rewrites the skill (see :meth:`register_skill`), prompts
        rendered afterwards pick up the new instructions.

        Args:
            name: The name of the skill.
            **kwargs: Values for ``{placeholder}`` variables in the skill's
                content (see :meth:`Skill.render`). Unknown placeholders are
                left as-is.
        """
        skill = self.get_skill(name)
        return f'<skill name="{skill.name}">\n{skill.render(**kwargs)}\n</skill>'

    def register_skill(self, registry, skill_name: str, field_name: Optional[str] = None):
        """Register a skill's instructions as an optimizable field in a PromptRegistry.

        This wires the skill into EvoAgentX's self-evolving engine: optimizers
        (e.g. those built on ``evoagentx.optimizers.optimizer_core``) can then
        rewrite the skill like any other prompt. After optimization, call
        ``manager.get_skill(skill_name).save()`` to persist the best version
        back to its SKILL.md.

        Args:
            registry: A ``evoagentx.optimizers.optimizer_core.PromptRegistry``.
            skill_name: The name of the skill to register.
            field_name: Field name in the registry. Defaults to ``skill:<skill-name>``.

        Returns:
            The registered ``OptimizableField``.
        """
        field = self.get_skill(skill_name).as_optimizable_field(field_name)
        registry.register_field(field)
        return field
