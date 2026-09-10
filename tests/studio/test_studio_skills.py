"""Tests for Studio skills (backend/api/skills_api.py).

A skill is standing instructions a node follows, resolved into its system
prompt at run time. The store is the framework's own SKILL.md format, so a
Studio skill is a plain framework skill — that is what makes it survive an
export.

Pins the behaviours the rest of the system leans on:

1. Injection must not mutate the canvas task: the Inspector shows the prompt
   the user wrote, not the prompt the run assembled.
2. A skill deleted after a workflow referenced it must be skipped at run time,
   not fail the run — saving is where the reference is checked.
"""

from conftest import make_task


class TestStore:
    def test_saved_skill_is_a_framework_skill(self, studio_data):
        from backend.api import skills_api
        from evoagentx.skills.skill import parse_skill_file

        skills_api.save_skill({"name": "rubric", "description": "How to grade.",
                               "content": "# Rubric\n\n- be strict"})
        path = skills_api.SKILLS_DIR / "rubric" / "SKILL.md"
        assert path.is_file()

        parsed = parse_skill_file(str(path))
        assert parsed.name == "rubric"
        assert parsed.description == "How to grade."
        assert "be strict" in parsed.content

    def test_overwrite_keeps_the_previous_version(self, studio_data):
        from backend.api import skills_api
        spec = {"name": "rubric", "description": "d", "content": "v1"}
        skills_api.save_skill(spec)
        skills_api.save_skill({**spec, "content": "v2"})

        assert skills_api.get_skill("rubric")["content"] == "v2"
        versions = list((skills_api.SKILLS_DIR / "rubric" / ".versions").glob("*.md"))
        assert versions, "overwriting should archive the previous SKILL.md"

    def test_invalid_names_and_empty_fields_are_refused(self, studio_data):
        import pytest
        from backend.api import skills_api
        for spec in (
            {"name": "9lives", "description": "d", "content": "c"},
            {"name": "has space", "description": "d", "content": "c"},
            {"name": "ok", "description": "", "content": "c"},
            {"name": "ok", "description": "d", "content": ""},
        ):
            with pytest.raises(skills_api.SkillError):
                skills_api.save_skill(spec)

    def test_a_hand_written_skill_does_not_break_the_listing(self, studio_data):
        """The framework's parser is forgiving: a SKILL.md with no usable
        frontmatter still loads, named after its folder. What matters here is
        that one odd folder never hides the skills around it."""
        from backend.api import skills_api
        skills_api.save_skill({"name": "good", "description": "d", "content": "c"})
        hand_written = skills_api.SKILLS_DIR / "hand_written"
        hand_written.mkdir(parents=True)
        (hand_written / "SKILL.md").write_text("Just some notes.\n", encoding="utf-8")

        listed = {s["name"] for s in skills_api.list_skills()}
        assert "good" in listed
        assert "hand_written" in listed

    def test_a_directory_without_a_skill_file_is_ignored(self, studio_data):
        from backend.api import skills_api
        skills_api.save_skill({"name": "good", "description": "d", "content": "c"})
        (skills_api.SKILLS_DIR / "not_a_skill").mkdir(parents=True)

        assert [s["name"] for s in skills_api.list_skills()] == ["good"]


class TestInjection:
    def test_skill_is_appended_to_the_system_prompt(self, studio_data):
        from backend.api import skills_api
        skills_api.save_skill({"name": "tone", "description": "d",
                               "content": "Be direct."})
        task = make_task("a", outputs=["x"], system_prompt="You are a writer.",
                         skill_names=["tone"])
        injected = skills_api.inject_into_tasks([task])[0]

        assert injected["system_prompt"].startswith("You are a writer.")
        assert "## Skill: tone" in injected["system_prompt"]
        assert "Be direct." in injected["system_prompt"]

    def test_the_canvas_task_is_left_alone(self, studio_data):
        from backend.api import skills_api
        skills_api.save_skill({"name": "tone", "description": "d", "content": "Be direct."})
        task = make_task("a", outputs=["x"], system_prompt="original",
                         skill_names=["tone"])
        skills_api.inject_into_tasks([task])
        assert task["system_prompt"] == "original"

    def test_a_deleted_skill_is_skipped_rather_than_failing_the_run(self, studio_data):
        from backend.api import skills_api
        task = make_task("a", outputs=["x"], system_prompt="original",
                         skill_names=["gone"])
        injected = skills_api.inject_into_tasks([task])[0]
        assert injected["system_prompt"] == "original"

    def test_tasks_without_skills_pass_through_untouched(self, studio_data):
        from backend.api import skills_api
        task = make_task("a", outputs=["x"])
        assert skills_api.inject_into_tasks([task])[0] is task

    def test_unknown_skill_names_are_rejected_at_save_time(self, studio_data):
        import pytest
        from backend.api import skills_api
        with pytest.raises(skills_api.SkillError):
            skills_api.validate_skill_names([make_task("a", skill_names=["ghost"])])
