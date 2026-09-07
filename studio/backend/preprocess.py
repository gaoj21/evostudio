"""Input preprocessing for EvoAgentX Studio.

A workflow may name a custom tool as its preprocessor. Every input record
passes through it before the workflow sees it — for a batch that is each
record, for a single run the inputs dict — so cleaning, renaming or deriving
fields happens once, in one place, and travels with the exported project.

The preprocessor is an ordinary custom tool (`def run(record): ...` returning a
dict), which means it is written in the same editor, executes in the same
subprocess with the same timeout, and is bundled by the same export path.
"""

import custom_tools


class PreprocessError(Exception):
    """User-facing preprocessing failure (HTTP 422)."""


def tool_name(graph: dict) -> str | None:
    name = (graph or {}).get("preprocess")
    return name.strip() if isinstance(name, str) and name.strip() else None


def _single_param(tool: dict) -> str:
    params = tool.get("params") or []
    if len(params) != 1:
        raise PreprocessError(
            f"Preprocessor '{tool['name']}' must take exactly one parameter "
            f"(the record); it takes {[p.get('name') for p in params]}."
        )
    return params[0]["name"]


def apply(graph: dict, records: list[dict]) -> list[dict]:
    """Run the graph's preprocessor over each record.

    Returns the records unchanged when no preprocessor is configured. A
    preprocessor that fails, or returns something that is not an object, stops
    the run: silently feeding the workflow unprocessed data would be worse than
    refusing, because the results would look fine and be wrong.
    """
    name = tool_name(graph)
    if not name:
        return records

    found = custom_tools.find(name)
    if found is None:
        raise PreprocessError(
            f"Preprocessor '{name}' is not among the custom tools. Create it, or "
            "clear the workflow's preprocess setting."
        )
    param = _single_param(found[1])

    out = []
    for index, record in enumerate(records, start=1):
        result = custom_tools.run_custom_tool(name, {param: record})
        if isinstance(result, dict) and "error" in result and "result" not in result:
            raise PreprocessError(
                f"Preprocessor '{name}' failed on record {index}: {result['error']}"
            )
        value = result.get("result") if isinstance(result, dict) else result
        if not isinstance(value, dict):
            raise PreprocessError(
                f"Preprocessor '{name}' must return an object; record {index} "
                f"produced {type(value).__name__}."
            )
        out.append(value)
    return out
