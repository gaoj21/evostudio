"""What counts as a tool.

A tool is a documented calling interface, not necessarily a function someone
wrote for the purpose: a library or an existing project is a tool too, as long
as it exposes an API and says what the API does. So a stored item is a Python
module — a toolkit — and every public function in it is one tool. Everything
the model is told is read back out of the code, so the code and the
description cannot drift apart.
"""

import pytest

from backend.api import custom_tools
from backend.api.custom_tools import CustomToolError


def save(code, name=None, builtins=()):
    spec = {"code": code}
    if name:
        spec["name"] = name
    return custom_tools.save_custom_tool(
        custom_tools.validate_spec(spec, list(builtins))
    )


ONE = '''
def word_count(text: str) -> dict:
    """Count the words in a piece of text.

    Args:
        text: the text to measure
    """
    return {"words": len(text.split())}
'''

TWO = '''"""Text statistics.

More detail that is not part of the summary.
"""


def word_count(text: str) -> dict:
    """Count the words in a piece of text.

    Args:
        text: the text to measure
    """
    return {"words": len(_tokens(text))}


def longest(text: str, minimum: int) -> dict:
    """The longest word, ignoring short ones.

    Args:
        text: the text to search
        minimum: ignore words shorter than this
    """
    return {"word": max(_tokens(text), key=len)}


def _tokens(text):
    return text.split()
'''


class TestOneFunction:
    def test_the_code_is_the_whole_definition(self, studio_data):
        spec = save(ONE)
        assert spec["name"] == "word_count"
        assert spec["description"] == "Count the words in a piece of text."
        [tool] = spec["tools"]
        assert tool["params"] == [
            {"name": "text", "type": "string", "description": "the text to measure"}
        ]

    def test_types_come_from_the_annotations(self, studio_data):
        spec = save('''
def measure(text: str, limit: int, ratio: float, deep: bool,
            options: dict, tags: list) -> dict:
    """Measure a text."""
    return {}
''')
        assert [(p["name"], p["type"]) for p in spec["tools"][0]["params"]] == [
            ("text", "string"), ("limit", "integer"), ("ratio", "number"),
            ("deep", "boolean"), ("options", "object"), ("tags", "array"),
        ]

    def test_a_parametrised_annotation_is_read_as_its_container(self, studio_data):
        spec = save('''
def tally(items: list[str]) -> dict:
    """Tally some items."""
    return {}
''')
        assert spec["tools"][0]["params"][0]["type"] == "array"

    def test_a_function_with_no_arguments_is_a_tool_too(self, studio_data):
        spec = save('''
def now() -> dict:
    """The current time."""
    return {}
''')
        assert spec["tools"][0]["params"] == []


class TestAModuleOfTools:
    def test_every_public_function_is_a_tool(self, studio_data):
        spec = save(TWO, name="text_stats")
        assert [t["name"] for t in spec["tools"]] == ["word_count", "longest"]

    def test_the_module_docstring_describes_the_toolkit(self, studio_data):
        spec = save(TWO, name="text_stats")
        # The summary only — the rest is detail for whoever reads the code.
        assert spec["description"] == "Text statistics."

    def test_a_leading_underscore_marks_a_helper(self, studio_data):
        # A toolkit is free to have internals, the way any module is.
        spec = save(TWO, name="text_stats")
        assert "_tokens" not in [t["name"] for t in spec["tools"]]

    def test_each_tool_keeps_its_own_parameters(self, studio_data):
        spec = save(TWO, name="text_stats")
        by_name = {t["name"]: t for t in spec["tools"]}
        assert [p["name"] for p in by_name["word_count"]["params"]] == ["text"]
        assert [p["name"] for p in by_name["longest"]["params"]] == ["text", "minimum"]

    def test_a_module_of_several_needs_a_name_to_group_them_under(self, studio_data):
        with pytest.raises(CustomToolError) as raised:
            save(TWO)
        assert "needs a name" in str(raised.value)

    def test_one_function_names_its_own_toolkit(self, studio_data):
        assert save(ONE)["name"] == "word_count"


class TestWrappingSomethingThatExists:
    def test_a_module_may_import_a_library_it_did_not_write(self, studio_data):
        # The point of the model: what makes it a tool is the description, not
        # who wrote the code underneath.
        spec = save('''"""Read PDFs with pypdf."""

from pypdf import PdfReader


def page_count(path: str) -> dict:
    """How many pages a PDF has.

    Args:
        path: path to the file
    """
    return {"pages": len(PdfReader(path).pages)}
''', name="pdf")
        assert spec["tools"][0]["name"] == "page_count"

    def test_module_level_setup_does_not_become_a_tool(self, studio_data):
        spec = save('''"""A project on the path."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path("/somewhere")))

CONSTANT = 3


def ask(question: str) -> dict:
    """Ask the project something.

    Args:
        question: what to ask
    """
    return {}
''', name="project")
        assert [t["name"] for t in spec["tools"]] == ["ask"]


class TestRefusals:
    """Each refusal names the thing to change; none of them guesses."""

    def test_a_function_with_no_docstring(self, studio_data):
        with pytest.raises(CustomToolError) as raised:
            save("def mystery(x: str) -> dict:\n    return {}\n")
        assert "needs a docstring" in str(raised.value)

    def test_an_unannotated_parameter(self, studio_data):
        # Guessing `string` would have the model passing the wrong thing and
        # the failure surfacing somewhere else entirely.
        with pytest.raises(CustomToolError) as raised:
            save('def counter(text) -> dict:\n    """Count."""\n    return {}\n')
        assert "no type annotation" in str(raised.value)

    def test_a_type_with_no_json_equivalent(self, studio_data):
        with pytest.raises(CustomToolError) as raised:
            save('def when(moment: complex) -> dict:\n    """T."""\n    return {}\n')
        assert "complex" in str(raised.value)

    def test_star_args(self, studio_data):
        with pytest.raises(CustomToolError) as raised:
            save('def f(*args: str) -> dict:\n    """F."""\n    return {}\n')
        assert "named" in str(raised.value)

    def test_an_async_function(self, studio_data):
        with pytest.raises(CustomToolError) as raised:
            save('async def fetch(url: str) -> dict:\n    """F."""\n    return {}\n')
        assert "async" in str(raised.value)

    def test_a_module_that_exposes_nothing(self, studio_data):
        with pytest.raises(CustomToolError) as raised:
            save('"""Nothing."""\n\n\ndef _helper(x):\n    return x\n', name="empty")
        assert "exposes nothing to call" in str(raised.value)

    def test_code_that_does_not_compile(self, studio_data):
        with pytest.raises(CustomToolError) as raised:
            save("def broken(x: str) -> dict\n    return {}\n")
        assert "does not compile" in str(raised.value)

    def test_a_name_a_built_in_already_has(self, studio_data):
        with pytest.raises(CustomToolError):
            save(ONE, name="FileToolkit", builtins=["FileToolkit"])

    def test_a_tool_name_another_toolkit_already_exports(self, studio_data):
        # A node refers to a tool by name alone, so a duplicate would be
        # resolved to whichever toolkit happened to be read first.
        save(ONE)
        with pytest.raises(CustomToolError) as raised:
            save('"""Other."""\n\n\ndef word_count(text: str) -> dict:\n'
                 '    """Clashes."""\n    return {}\n', name="other")
        assert "already exported" in str(raised.value)

    def test_re_saving_a_toolkit_does_not_clash_with_itself(self, studio_data):
        save(TWO, name="text_stats")
        save(TWO, name="text_stats")      # editing it must not be a conflict


class TestLookup:
    def test_a_tool_is_found_by_its_own_name(self, studio_data):
        save(TWO, name="text_stats")
        toolkit, tool = custom_tools.find("longest")
        assert toolkit["name"] == "text_stats"
        assert tool["name"] == "longest"

    def test_an_unknown_name_is_not_found(self, studio_data):
        assert custom_tools.find("nope") is None

    def test_every_tool_is_enumerable(self, studio_data):
        save(TWO, name="text_stats")
        save(ONE.replace("word_count", "counter"))
        assert {t["name"] for _, t in custom_tools.iter_tools()} == {
            "word_count", "longest", "counter"
        }

    def test_the_older_single_function_shape_is_still_readable(self, studio_data):
        # A spec stored before a toolkit could hold more than one tool.
        legacy = {"name": "old", "description": "An older tool.",
                  "params": [{"name": "text", "type": "string", "description": ""}],
                  "code": "def run(text):\n    return {}\n"}
        [tool] = custom_tools.tools_of(legacy)
        assert tool["name"] == "old"
        assert [p["name"] for p in tool["params"]] == ["text"]


class TestExecution:
    def test_a_tool_runs_and_returns_its_value(self, studio_data):
        save(TWO, name="text_stats")
        assert custom_tools.run_custom_tool(
            "word_count", {"text": "one two three"}
        ) == {"result": {"words": 3}}

    def test_each_tool_of_a_toolkit_is_reachable(self, studio_data):
        save(TWO, name="text_stats")
        out = custom_tools.run_custom_tool(
            "longest", {"text": "a bb cccc", "minimum": 1})
        assert out == {"result": {"word": "cccc"}}

    def test_a_helper_is_available_to_the_tool_that_uses_it(self, studio_data):
        # The whole module is executed, so internals are in scope.
        save(TWO, name="text_stats")
        assert "error" not in custom_tools.run_custom_tool(
            "word_count", {"text": "x y"})

    def test_a_failing_tool_reports_why(self, studio_data):
        save('def boom(x: str) -> dict:\n    """Fails."""\n'
             "    raise ValueError('nope')\n")
        out = custom_tools.run_custom_tool("boom", {"x": "a"})
        assert "nope" in out["error"]

    def test_an_unknown_tool_is_reported_not_raised(self, studio_data):
        assert "not found" in custom_tools.run_custom_tool("nope", {})["error"]


class TestTheFrameworkToolkit:
    def test_it_holds_every_tool_the_module_exports(self, studio_data):
        spec = save(TWO, name="text_stats")
        toolkit = custom_tools.make_toolkit(spec)

        assert toolkit.name == "text_stats"
        assert toolkit.get_tool_names() == ["word_count", "longest"]

    def test_each_tool_declares_its_own_schema(self, studio_data):
        spec = save(TWO, name="text_stats")
        tool = custom_tools.make_toolkit(spec).get_tool("longest")

        assert tool.description == "The longest word, ignoring short ones."
        assert tool.inputs["minimum"]["type"] == "integer"
        assert tool.required == ["text", "minimum"]

    def test_a_toolkit_can_be_built_more_than_once(self, studio_data):
        # Every run builds its own toolkit instances. The framework registers
        # each Tool subclass in a process-wide registry and rejects a repeat,
        # so a second run of the same workflow used to fail to resolve its
        # tools at all.
        spec = save(TWO, name="text_stats")
        first = custom_tools.make_toolkit(spec)
        second = custom_tools.make_toolkit(spec)
        assert first.get_tool_names() == second.get_tool_names()
        assert second.get_tool("word_count")(text="a b") == {"result": {"words": 2}}

    def test_an_edited_tool_replaces_the_old_schema(self, studio_data):
        save(ONE)
        custom_tools.make_toolkit(custom_tools.find("word_count")[0])
        save(ONE.replace("(text: str)", "(text: str, limit: int)")
                .replace("        text: the text to measure",
                         "        text: the text to measure\n        limit: a cap"))
        toolkit = custom_tools.make_toolkit(custom_tools.find("word_count")[0])
        assert toolkit.get_tool("word_count").required == ["text", "limit"]

    def test_calling_through_the_framework_object_works(self, studio_data):
        spec = save(TWO, name="text_stats")
        tool = custom_tools.make_toolkit(spec).get_tool("word_count")
        assert tool(text="one two") == {"result": {"words": 2}}


class TestTheExportedProject:
    """A deployed project has to resolve tools the same way the canvas does."""

    @staticmethod
    def build(graph):
        import io
        import zipfile

        from backend.api import export_api
        _, payload = export_api.build_project(graph)
        archive = zipfile.ZipFile(io.BytesIO(payload))
        return {n.split("/", 1)[1]: archive.read(n).decode("utf-8")
                for n in archive.namelist()
                if "/" in n and not n.endswith("/")}

    @staticmethod
    def graph(tool_name, tool_names=()):
        return {
            "id": "g1", "name": "G", "goal": "do a thing",
            "tasks": [
                {"name": "step", "kind": "tool", "tool": tool_name,
                 "description": "a tool node",
                 "inputs": [{"name": "text", "type": "str", "description": "t",
                             "required": True}],
                 "outputs": [{"name": "out", "type": "str", "description": "o",
                              "required": True}]},
                {"name": "say", "description": "describe it",
                 "inputs": [{"name": "out", "type": "str", "description": "o",
                             "required": True}],
                 "outputs": [{"name": "verdict", "type": "str", "description": "v",
                              "required": True}],
                 "prompt": "Out: {out}", "parse_mode": "str",
                 "tool_names": list(tool_names)},
            ],
            "edges": [{"source": "step", "target": "say"}],
        }

    def test_every_tool_of_a_bundled_toolkit_travels(self, studio_data):
        save(TWO, name="text_stats")
        files = self.build(self.graph("word_count", ["text_stats"]))
        import json as _json

        spec = _json.loads(files["tools/text_stats.json"])
        assert [t["name"] for t in spec["tools"]] == ["word_count", "longest"]
        assert spec["description"] == "Text statistics."

    def test_a_tool_node_records_the_toolkit_holding_its_tool(self, studio_data):
        # A node names a tool. Passing that name where a toolkit name is wanted
        # made the project exit with "Unknown toolkit" the moment it ran.
        save(TWO, name="text_stats")
        source = self.build(self.graph("longest", ["text_stats"]))["workflow.py"]
        assert "'toolkit': 'text_stats'" in source

    def test_a_built_in_tool_node_records_its_toolkit_too(self, studio_data):
        # `read_file` is a tool inside FileToolkit and is not a toolkit class,
        # so an exported project could never run one of these either.
        source = self.build(self.graph("read_file"))["workflow.py"]
        assert "'toolkit': 'FileToolkit'" in source

    def test_the_project_builds_each_tool_class_once(self, studio_data):
        # Same process-wide registry, same duplicate-module refusal: a second
        # run, or any batch, would have failed to resolve its tools.
        save(TWO, name="text_stats")
        source = self.build(self.graph("word_count", ["text_stats"]))["workflow.py"]
        assert "_TOOL_CLASSES" in source
        assert 'cls_name = f"{tool[\'name\']}_' in source
