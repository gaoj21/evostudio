import os

import pytest

from evoagentx.skills import SkillManager, parse_skill_file

try:
    from evoagentx.skills import SkillToolkit
    TOOLS_AVAILABLE = True
except ImportError:  # optional `tools` dependencies not installed
    TOOLS_AVAILABLE = False


def write_skill(root, dir_name, frontmatter, body, extra_files=None):
    skill_dir = os.path.join(root, dir_name)
    os.makedirs(skill_dir, exist_ok=True)
    with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(frontmatter + body)
    for rel_path, content in (extra_files or {}).items():
        file_path = os.path.join(skill_dir, rel_path)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
    return skill_dir


@pytest.fixture
def skills_root(tmp_path):
    root = str(tmp_path / "skills")
    write_skill(
        root,
        "code-review",
        "---\nname: code-review\ndescription: Review code for issues.\n---\n",
        "# Code Review\n\nStep 1: read the code.\n",
        extra_files={"references/checklist.md": "- no secrets\n"},
    )
    write_skill(
        root,
        "commit",
        "---\nname: commit\ndescription: Write a git commit message.\n---\n",
        "# Commit\n\nUse conventional commits.\n",
    )
    return root


class TestParseSkillFile:
    def test_parse_frontmatter(self, skills_root):
        skill = parse_skill_file(os.path.join(skills_root, "code-review", "SKILL.md"))
        assert skill.name == "code-review"
        assert skill.description == "Review code for issues."
        assert "Step 1: read the code." in skill.content

    def test_fallback_without_frontmatter(self, tmp_path):
        skill_dir = write_skill(str(tmp_path), "plain-skill", "", "# Plain\n\nDo plain things.\n")
        skill = parse_skill_file(os.path.join(skill_dir, "SKILL.md"))
        assert skill.name == "plain-skill"  # falls back to directory name
        assert skill.description == "Plain"  # falls back to first body line

    def test_get_resources(self, skills_root):
        skill = parse_skill_file(os.path.join(skills_root, "code-review", "SKILL.md"))
        assert skill.get_resources() == ["references/checklist.md"]


class TestSkillManager:
    def test_discover(self, skills_root):
        manager = SkillManager(skills_root)
        names = [s["name"] for s in manager.list_skills()]
        assert sorted(names) == ["code-review", "commit"]

    def test_load_skill(self, skills_root):
        manager = SkillManager(skills_root)
        loaded = manager.load_skill("commit")
        assert loaded["name"] == "commit"
        assert "conventional commits" in loaded["instructions"]
        assert loaded["resources"] == []

    def test_load_unknown_skill(self, skills_root):
        manager = SkillManager(skills_root)
        with pytest.raises(ValueError, match="not found"):
            manager.load_skill("nope")

    def test_single_skill_dir_and_file(self, skills_root):
        manager = SkillManager(os.path.join(skills_root, "commit"))
        assert manager.list_skills()[0]["name"] == "commit"

        manager = SkillManager(os.path.join(skills_root, "code-review", "SKILL.md"))
        assert manager.list_skills()[0]["name"] == "code-review"


@pytest.mark.skipif(not TOOLS_AVAILABLE, reason="optional `tools` dependencies not installed")
class TestSkillToolkit:
    def test_tools(self, skills_root):
        toolkit = SkillToolkit(skill_paths=skills_root)
        assert toolkit.get_tool_names() == ["list_skills", "load_skill"]

        result = toolkit.get_tool("list_skills")()
        assert len(result["skills"]) == 2

        loaded = toolkit.get_tool("load_skill")(name="code-review")
        assert loaded["resources"] == ["references/checklist.md"]

    def test_load_unknown_skill_returns_error(self, skills_root):
        toolkit = SkillToolkit(skill_paths=skills_root)
        result = toolkit.get_tool("load_skill")(name="nope")
        assert "error" in result

    def test_tool_schemas(self, skills_root):
        toolkit = SkillToolkit(skill_paths=skills_root)
        schemas = toolkit.get_tool_schemas()
        assert {s["function"]["name"] for s in schemas} == {"list_skills", "load_skill"}


class TestSkillEvolution:
    """Skill content can be optimized like a prompt and saved back to SKILL.md."""

    def test_optimizable_field_get_set(self, skills_root):
        manager = SkillManager(skills_root)
        skill = manager.get_skill("commit")
        field = skill.as_optimizable_field()
        assert field.name == "skill:commit"
        assert "conventional commits" in field.get()

        field.set("Always use conventional commits. Include a scope.")
        assert skill.content == "Always use conventional commits. Include a scope."

    def test_register_skill(self, skills_root):
        from evoagentx.optimizers.optimizer_core import PromptRegistry

        manager = SkillManager(skills_root)
        registry = PromptRegistry()
        manager.register_skill(registry, "commit")

        assert registry.names() == ["skill:commit"]
        registry.set("skill:commit", "New instructions.")
        assert manager.get_skill("commit").content == "New instructions."

    def test_skill_prompt_reflects_updates(self, skills_root):
        manager = SkillManager(skills_root)
        manager.get_skill("commit").as_optimizable_field().set("Rewritten body.")
        prompt = manager.get_skill_prompt("commit")
        assert prompt == '<skill name="commit">\nRewritten body.\n</skill>'

    def test_save_roundtrip(self, tmp_path):
        skill_dir = write_skill(
            str(tmp_path),
            "versioned",
            "---\nname: versioned\ndescription: A skill.\nlicense: MIT\n---\n",
            "# Versioned\n\nv1 instructions.\n",
        )
        manager = SkillManager(skill_dir)
        skill = manager.get_skill("versioned")
        skill.content = "# Versioned\n\nv2 instructions (optimized).\n"
        saved_path = skill.save()

        reloaded = parse_skill_file(saved_path)
        assert reloaded.name == "versioned"
        assert reloaded.description == "A skill."
        assert reloaded.metadata.get("license") == "MIT"  # extra frontmatter preserved
        assert "v2 instructions (optimized)." in reloaded.content


class TestSkillEvolutionGlue:
    """Glue between skills and the workflow optimizers (e.g. TextGrad)."""

    IO_SPEC = {
        "inputs": [{"name": "problem", "type": "str", "required": True, "description": "The problem."}],
        "outputs": [{"name": "answer", "type": "str", "required": True, "description": "The answer."}],
    }

    def test_skill_to_graph(self, skills_root):
        from evoagentx.skills import skill_to_graph

        manager = SkillManager(skills_root)
        skill = manager.get_skill("code-review")
        graph = skill_to_graph(skill, instruction="Review this code:\n{problem}", **self.IO_SPEC)

        assert len(graph.nodes) == 1
        assert graph.nodes[0].name == "code_review_task"
        agent = graph.nodes[0].agents[0]
        assert agent["system_prompt"] == skill.content

    def test_update_skill_from_graph(self, skills_root):
        from evoagentx.skills import skill_to_graph, update_skill_from_graph

        manager = SkillManager(skills_root)
        skill = manager.get_skill("code-review")
        graph = skill_to_graph(skill, instruction="Review this code:\n{problem}", **self.IO_SPEC)

        # simulate an optimizer rewriting the agent's system prompt
        graph.nodes[0].agents[0]["system_prompt"] = "Optimized instructions."
        update_skill_from_graph(skill, graph)
        assert skill.content == "Optimized instructions."

    def test_list_benchmark(self):
        from evoagentx.skills import ListBenchmark

        data = [
            {"problem": "1+1=?", "label": "2"},
            {"problem": "2+2=?", "label": "4"},
        ]
        benchmark = ListBenchmark("math_toy", data)
        assert benchmark.get_train_data() == data
        assert benchmark.get_labels(data) == ["2", "4"]
        assert benchmark.evaluate(prediction="2", label="2") == {"accuracy": 1.0, "em": 1.0}
        assert benchmark.evaluate(prediction="3", label="2") == {"accuracy": 0.0, "em": 0.0}

        with_ids = ListBenchmark("math_ids", [{"id": "a", "problem": "x", "label": "y"}], id_key="id")
        assert with_ids.get_example_by_id("a")["label"] == "y"


class FakeLLM:
    """Minimal stand-in for a BaseLLM, for offline program tests."""

    class _Response:
        def __init__(self, content):
            self.content = content

    def generate(self, prompt, **kwargs):
        return self._Response(f"answer:{prompt}")


class TestSkillRendering:
    def test_render_with_placeholders(self, tmp_path):
        skill_dir = write_skill(
            str(tmp_path),
            "tpl",
            "---\nname: tpl\ndescription: Templated skill.\n---\n",
            "Review the {language} code. Keep it {style}.",
        )
        from evoagentx.skills import SkillManager
        manager = SkillManager(skill_dir)
        skill = manager.get_skill("tpl")

        assert skill.render(language="Python", style="simple") == "Review the Python code. Keep it simple."
        # unknown placeholders are left as-is
        assert skill.render(language="Python") == "Review the Python code. Keep it {style}."
        # no kwargs -> content untouched
        assert skill.render() == skill.content

    def test_get_skill_prompt_with_kwargs(self, tmp_path):
        skill_dir = write_skill(
            str(tmp_path),
            "tpl",
            "---\nname: tpl\ndescription: Templated skill.\n---\n",
            "Review the {language} code.",
        )
        from evoagentx.skills import SkillManager
        manager = SkillManager(skill_dir)
        prompt = manager.get_skill_prompt("tpl", language="Go")
        assert prompt == '<skill name="tpl">\nReview the Go code.\n</skill>'


class TestSkillVersioning:
    def test_save_with_backup(self, tmp_path):
        skill_dir = write_skill(
            str(tmp_path),
            "ver",
            "---\nname: ver\ndescription: Versioned skill.\n---\n",
            "v1 instructions.",
        )
        from evoagentx.skills import SkillManager
        manager = SkillManager(skill_dir)
        skill = manager.get_skill("ver")

        skill.content = "v2 instructions."
        skill.save(backup=True)

        versions_dir = os.path.join(skill_dir, ".versions")
        backups = os.listdir(versions_dir)
        assert len(backups) == 1
        with open(os.path.join(versions_dir, backups[0]), encoding="utf-8") as f:
            assert "v1 instructions." in f.read()

        # current file has the new content; backup dir is hidden -> not a resource
        assert "v2 instructions." in skill.content
        assert skill.get_resources() == []

    def test_save_without_backup_keeps_no_history(self, tmp_path):
        skill_dir = write_skill(
            str(tmp_path),
            "ver",
            "---\nname: ver\ndescription: Versioned skill.\n---\n",
            "v1 instructions.",
        )
        from evoagentx.skills import SkillManager
        skill = SkillManager(skill_dir).get_skill("ver")
        skill.content = "v2 instructions."
        skill.save()
        assert not os.path.exists(os.path.join(skill_dir, ".versions"))


class TestSkillPrograms:
    def test_skill_program_call_and_update(self, tmp_path):
        from evoagentx.skills import SkillManager, SkillProgram

        skill_dir = write_skill(
            str(tmp_path),
            "prog",
            "---\nname: prog\ndescription: Program skill.\n---\n",
            "Original instructions.",
        )
        skill = SkillManager(skill_dir).get_skill("prog")
        program = SkillProgram(
            skill, FakeLLM(), instruction="Task:\n{problem}", input_names=["problem"], output_name="solution"
        )

        prediction, execution_data = program(problem="1+1=?")
        assert prediction.startswith("answer:Original instructions.")
        assert "Task:\n1+1=?" in prediction
        assert execution_data == {"problem": "1+1=?", "solution": prediction}

        # simulate the optimizer rewriting the prompt
        program.prompt = "Optimized instructions."
        program.update_skill()
        assert skill.content == "Optimized instructions."

    def test_skill_program_save_load(self, tmp_path):
        from evoagentx.skills import SkillManager, SkillProgram

        skill_dir = write_skill(
            str(tmp_path), "prog", "---\nname: prog\ndescription: d.\n---\n", "v1."
        )
        skill = SkillManager(skill_dir).get_skill("prog")
        program = SkillProgram(skill, FakeLLM(), instruction="{problem}", input_names=["problem"])

        state_file = str(tmp_path / "program.json")
        program.prompt = "v2."
        program.save(state_file)

        program.prompt = "v3."
        program.load(state_file)
        assert program.prompt == "v2."

    def test_skill_evo_program(self, tmp_path):
        from evoagentx.skills import SkillEvoProgram, SkillManager

        skill_dir = write_skill(
            str(tmp_path), "evo", "---\nname: evo\ndescription: d.\n---\n", "seed version."
        )
        skill = SkillManager(skill_dir).get_skill("evo")
        program = SkillEvoProgram(skill, FakeLLM(), candidates=["variant A.", "variant B."])
        assert program.candidates == ["seed version.", "variant A.", "variant B."]

        prediction, meta = program(input="do the task")
        assert "seed version." in prediction and "do the task" in prediction
        assert "full_prompt" in meta

        # simulate optimizer finishing with a best candidate (string form)
        program.candidates = "evolved version."
        program.update_skill()
        assert skill.content == "evolved version."

        # save/load roundtrip
        state_file = str(tmp_path / "evo_program.json")
        program.save(state_file)
        program.candidates = ["other."]
        program.load(state_file)
        assert program.candidates == "evolved version."

    def test_make_evoprompt_optimizer(self, tmp_path):
        from evoagentx.models import OpenAILLMConfig
        from evoagentx.skills import SkillEvoProgram, SkillManager, make_evoprompt_optimizer

        skill_dir = write_skill(
            str(tmp_path), "evo", "---\nname: evo\ndescription: d.\n---\n", "seed version."
        )
        skill = SkillManager(skill_dir).get_skill("evo")
        program = SkillEvoProgram(skill, FakeLLM())

        optimizer = make_evoprompt_optimizer(
            program,
            evo_llm_config=OpenAILLMConfig(model="gpt-4o-mini", openai_key="fake-key"),
            algorithm="GA",
            population_size=4,
            iterations=2,
            enable_logging=False,
        )
        assert optimizer.population_size == 4
        assert optimizer.registry.get("skill_versions") == ["seed version."]

    def test_make_mipro_optimizer(self, tmp_path):
        dspy = pytest.importorskip("dspy", reason="optional `optimizers` dependencies not installed")
        from evoagentx.models import OpenAILLMConfig, OpenAILLM
        from evoagentx.skills import SkillManager, SkillProgram, make_mipro_optimizer

        skill_dir = write_skill(
            str(tmp_path), "prog", "---\nname: prog\ndescription: d.\n---\n", "seed instructions."
        )
        skill = SkillManager(skill_dir).get_skill("prog")
        program = SkillProgram(skill, FakeLLM(), instruction="{problem}", input_names=["problem"])

        optimizer = make_mipro_optimizer(
            program,
            optimizer_llm=OpenAILLM(config=OpenAILLMConfig(model="gpt-4o-mini", openai_key="fake-key")),
            auto="light",
        )
        assert optimizer.program is program


def _make_simple_graph():
    from evoagentx.workflow.workflow_graph import SequentialWorkFlowGraph

    return SequentialWorkFlowGraph.from_dict({
        "goal": "Answer the question.",
        "tasks": [{
            "name": "answer",
            "description": "Answer the question.",
            "inputs": [{"name": "problem", "type": "str", "required": True, "description": "The problem."}],
            "outputs": [{"name": "answer", "type": "str", "required": True, "description": "The answer."}],
            "prompt": "Answer: {problem}",
        }],
    })


class TestWorkflowSearch:
    def test_program_roundtrip(self, tmp_path):
        from evoagentx.skills import WorkflowProgram

        graph = _make_simple_graph()
        program = WorkflowProgram(graph=graph)
        rebuilt = program.build_graph()
        assert rebuilt.goal == graph.goal
        assert len(rebuilt.nodes) == 1

        path = str(tmp_path / "wf.json")
        program.save(path)
        loaded = WorkflowProgram.from_file(path)
        assert loaded.build_graph().goal == graph.goal

        # backup archives the previous version
        program2 = WorkflowProgram(graph=graph)
        program2.save(path, backup=False)
        program2.save(path, backup=True)
        versions = os.listdir(os.path.join(str(tmp_path), ".versions"))
        assert len(versions) == 1

    def test_search_loop(self):
        import json as _json
        from evoagentx.skills import (
            ListBenchmark, WorkflowProgram, WorkflowSearchOptimizer,
        )

        program = WorkflowProgram(graph=_make_simple_graph())

        class StubEvaluator:
            def evaluate(self, graph, benchmark, eval_mode, update_agents=True, **kwargs):
                return {"score": 1.0 if "improved" in graph.goal else 0.5}

        class FakeProposerLLM:
            def __init__(self):
                self.calls = 0

            def generate(self, prompt, **kwargs):
                self.calls += 1

                class R: pass
                r = R()
                if self.calls == 1:
                    r.content = "this is not json at all"
                else:
                    config = _json.loads(program.workflow_text)
                    config["goal"] = "An improved goal."
                    r.content = _json.dumps(config)
                return r

        optimizer = WorkflowSearchOptimizer(
            program=program,
            evaluator=StubEvaluator(),
            optimizer_llm=FakeProposerLLM(),
            max_rounds=3,
        )
        benchmark = ListBenchmark("toy", [{"input": "x", "label": "y"}])
        result = optimizer.optimize(benchmark)

        assert result["best_score"] == 1.0
        assert "improved" in program.workflow_text
        notes = [h["note"] for h in result["history"]]
        assert any("unparseable" in n for n in notes)
        assert any("new best" in n for n in notes)
        # the best graph is still buildable
        assert "improved" in program.build_graph().goal

    def test_invalid_proposal_gets_feedback(self):
        from evoagentx.skills import WorkflowProgram, WorkflowSearchOptimizer

        program = WorkflowProgram(graph=_make_simple_graph())

        class StubEvaluator:
            def evaluate(self, graph, benchmark, eval_mode, update_agents=True, **kwargs):
                return {"score": 0.5}

        class BadStructureLLM:
            def generate(self, prompt, **kwargs):
                class R: pass
                r = R()
                r.content = '{"goal": "x", "nodes": "not-a-list"}'
                return r

        optimizer = WorkflowSearchOptimizer(
            program=program,
            evaluator=StubEvaluator(),
            optimizer_llm=BadStructureLLM(),
            max_rounds=2,
        )
        benchmark_data = [{"input": "x", "label": "y"}]
        from evoagentx.skills import ListBenchmark
        result = optimizer.optimize(ListBenchmark("toy", benchmark_data))

        # search degrades gracefully: best stays the initial workflow
        assert result["best_score"] == 0.5
        assert all("invalid" in h["note"] or h["round"] == 0 for h in result["history"])
        assert program.build_graph().goal == "Answer the question."


class TestEvaluatorThreadManagerCache:
    """Regression: Evaluator caches per-thread agent managers keyed by thread id.
    Since Python recycles ids of dead threads across ThreadPoolExecutor instances,
    a stale cached manager (without newly added agents) could be resurrected on
    later evaluate() calls. evaluate() must drop the cache so updated agents are seen."""

    def test_evaluate_clears_thread_agent_manager_cache(self):
        from evoagentx.evaluators import Evaluator
        from evoagentx.agents.agent_manager import AgentManager
        from evoagentx.models import LiteLLM, LiteLLMConfig
        from evoagentx.skills import ListBenchmark

        llm = LiteLLM(config=LiteLLMConfig(model="deepseek/deepseek-v4-flash", deepseek_key="fake"))
        evaluator = Evaluator(
            llm=llm,
            agent_manager=AgentManager(),
            collate_func=lambda x: {"problem": x["problem"]},
            num_workers=2,
        )
        # simulate a stale cached manager left by a recycled thread id
        evaluator._thread_agent_managers[999] = AgentManager()

        graph = _make_simple_graph()
        benchmark = ListBenchmark("toy", [{"problem": "1+1?", "label": "2"}])
        # execution itself fails on the fake API key; we only care about the cache
        evaluator.evaluate(graph=graph, benchmark=benchmark, eval_mode="dev", update_agents=True)

        assert 999 not in evaluator._thread_agent_managers
