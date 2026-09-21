"""Palette presets and workflow templates.

Built-in Input presets come from the source types; everything task-specific
(preset nodes, starter templates) comes from project plugins
(backend/features/plugins.py).
"""

from backend.features import plugins


def _io(name, type_="str", description="", required=True):
    return {"name": name, "type": type_, "description": description, "required": required}



def source_presets() -> list[dict]:
    """Canvas input-source presets (kind="source" — executed in Python, not LLM)."""
    from .source_apis import all_source_types

    presets = []
    for type_, schema in all_source_types().items():
        presets.append({
            "type": f"source_{type_}",
            "label": schema["label"],
            "description": schema["description"],
            "defaults": {
                "kind": "source",
                "description": schema["description"],
                "source": {
                    "type": type_,
                    **{f["name"]: f.get("default", "") for f in schema["config"]},
                    **({"loader":"python", "read_batch_size":100} if type_ == "dataloader" else {}),
                },
                "inputs": [],
                "outputs": [
                    _io(name, description=f"{schema['label']} output", required=False)
                    for name in schema["outputs"]
                ],
            },
        })
    return presets


def node_presets() -> list[dict]:
    """Preset nodes offered by project plugins, each tagged with its group."""
    return plugins.presets()


def templates() -> list[dict]:
    return plugins.templates()
