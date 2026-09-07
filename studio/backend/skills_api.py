"""Skills for EvoAgentX Studio.

A skill is a folder of Markdown instructions following the Agent Skills
convention: studio/data/skills/<name>/SKILL.md, with YAML frontmatter
(`name`, `description`) and a Markdown body. Parsing and writing go through
the framework's `evoagentx.skills` so a Studio skill is a plain framework
skill -- the same folder can be handed to SkillManager anywhere else, and
saving keeps the previous version under `.versions/`.

Skills differ from tools: a tool is code the workflow *calls*, a skill is
standing instructions a node *follows* (a taxonomy, a rubric, a house style).
Attaching one to an LLM node appends its content to that node's system
prompt at run time -- deterministic, no extra LLM call -- which is how
credit_risk already injects its taxonomy and scoring rubric.

Routes: GET/POST/DELETE /api/skills (mounted via this module's router).
"""

import re
import shutil
import sys
import threading
from pathlib import Path
from studio_config import data_path

from fastapi import APIRouter, Body, HTTPException

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

SKILLS_DIR = data_path("skills")

_lock = threading.Lock()

# Delimiters around injected skills, so an operator reading a rendered system
# prompt can tell instantly which part came from a skill.
SKILL_HEADER = "## Skill: {name}"


class SkillError(Exception):
    """User-facing skill validation error (HTTP 422)."""


def _validate_name(name: str) -> str:
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]*", name or ""):
        raise SkillError(
            f"Invalid skill name {name!r}: must start with a letter and contain "
            "only letters, digits, underscores or hyphens"
        )
    return name


def _skill_dir(name: str) -> Path:
    return SKILLS_DIR / _validate_name(name)


def list_skills() -> list[dict]:
    """Every skill in the store, newest parse wins; unreadable ones are skipped."""
    from evoagentx.skills.skill import SKILL_FILE_NAME, parse_skill_file

    if not SKILLS_DIR.is_dir():
        return []
    out = []
    for entry in sorted(SKILLS_DIR.iterdir()):
        skill_file = entry / SKILL_FILE_NAME
        if not skill_file.is_file():
            continue
        try:
            skill = parse_skill_file(str(skill_file))
        except Exception:
            continue  # a hand-edited SKILL.md must not break the whole list
        out.append({
            "name": skill.name,
            "description": skill.description,
            "content": skill.content,
            "resources": skill.get_resources(),
        })
    return out


def get_skill(name: str) -> dict | None:
    for skill in list_skills():
        if skill["name"] == name:
            return skill
    return None


def save_skill(spec: dict) -> dict:
    """Create or overwrite a skill. Overwrites keep the previous SKILL.md."""
    from evoagentx.skills.skill import Skill

    name = _validate_name((spec.get("name") or "").strip())
    description = (spec.get("description") or "").strip()
    if not description:
        raise SkillError("Skill description is required (it is how agents choose a skill)")
    content = (spec.get("content") or "").strip()
    if not content:
        raise SkillError("Skill content is required (the instructions themselves)")

    with _lock:
        path = _skill_dir(name)
        existed = (path / "SKILL.md").is_file()
        path.mkdir(parents=True, exist_ok=True)
        skill = Skill(name=name, description=description, path=str(path), content=content)
        skill.save(backup=existed)
    return {"name": name, "description": description, "content": content, "resources": []}


def delete_skill(name: str) -> bool:
    with _lock:
        path = _skill_dir(name)
        if not path.is_dir():
            return False
        shutil.rmtree(path)
        return True


def inject_into_tasks(tasks: list[dict]) -> list[dict]:
    """Append each task's attached skills to its system prompt.

    Returns new task dicts: the caller's canvas tasks are left untouched, so
    what the user sees in the Inspector stays the prompt they wrote.
    Unknown skill names are skipped rather than failing the run -- a deleted
    skill should not make every workflow referencing it unrunnable.
    """
    by_name = {s["name"]: s for s in list_skills()}
    out = []
    for task in tasks:
        names = task.get("skill_names") or []
        if not names:
            out.append(task)
            continue
        blocks = [
            f"{SKILL_HEADER.format(name=by_name[n]['name'])}\n\n{by_name[n]['content']}"
            for n in names if n in by_name
        ]
        if not blocks:
            out.append(task)
            continue
        task = dict(task)
        base = (task.get("system_prompt") or "").strip()
        task["system_prompt"] = (base + "\n\n" if base else "") + "\n\n".join(blocks)
        out.append(task)
    return out


def validate_skill_names(tasks: list[dict]) -> None:
    """Reject tasks referencing skills that do not exist (save-time check)."""
    known = {s["name"] for s in list_skills()}
    unknown = sorted({
        name
        for task in tasks
        for name in (task.get("skill_names") or [])
        if name not in known
    })
    if unknown:
        raise SkillError(f"Unknown skill(s): {', '.join(unknown)}")


router = APIRouter(prefix="/api")


@router.get("/skills")
def list_skills_route():
    return {"skills": list_skills()}


@router.get("/skills/{name}")
def get_skill_route(name: str):
    skill = get_skill(name)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
    return skill


@router.post("/skills")
def save_skill_route(body: dict = Body(...)):
    try:
        return save_skill(body)
    except SkillError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.delete("/skills/{name}")
def delete_skill_route(name: str):
    if not delete_skill(name):
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
    return {"ok": True}
