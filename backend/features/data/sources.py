"""Batch input sources for EvoAgentX Studio.

Records become workflow inputs from:
- uploaded JSON/JSONL/CSV files (column names -> input names);
- canvas Input nodes: DataLoaders, user datasets, API connectors, custom
  toolkit sources, and Input types offered by project plugins.

Mapping rule (shared): a record fills a workflow input when the names match;
unmatched required inputs are a hard error, unmatched record fields ignored.
"""

class SourceError(Exception):
    """User-facing source/mapping error (returned as HTTP 422)."""


def normalize_eval_records(records: list[dict]) -> list[dict]:
    """Normalize uploaded dataset records to {"inputs", "label", "id"}.

    Accepts {"inputs": {...}, "label": ...} directly, or a flat record where
    every key except label/id is treated as an input.
    """
    normalized = []
    for i, record in enumerate(records, start=1):
        if "label" not in record:
            raise SourceError(f"Record {i}: missing 'label' field")
        inputs = record.get("inputs")
        if not isinstance(inputs, dict):
            inputs = {k: v for k, v in record.items() if k not in ("inputs", "label", "id")}
        normalized.append({
            "inputs": inputs,
            "label": record["label"],
            "id": record.get("id") or f"ex-{i}",
        })
    return normalized


# ---------------------------------------------------------------------------
# Uploaded JSONL / CSV
# ---------------------------------------------------------------------------

def parse_upload(filename: str, content: bytes) -> list[dict]:
    """One parser for every upload: the same header, row-length and encoding
    checks as saved datasets, so a ragged CSV fails here instead of feeding
    the workflow a record keyed by None."""
    from .user_datasets import parse
    records, _fields = parse(filename or "", content)
    return records


# ---------------------------------------------------------------------------
# Canvas source nodes
# ---------------------------------------------------------------------------

def records_from_source_node(node: dict) -> list[dict]:
    """Execute a canvas source node (kind="source"); returns its records.

    DataLoaders, user datasets and plugin Input types return many records;
    API connectors return one per execution.
    """
    config = node.get("source") or {}
    type_ = config.get("type")
    if type_ == "dataloader":
        from .dataloaders import records
        return records(config)
    if type_ == "user_dataset":
        from backend.features.data.user_datasets import records
        return records(config)
    from backend.features import plugins
    plugin = plugins.source_type(type_)
    if plugin is not None:
        records = plugin["records"](config)
        if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
            raise SourceError(f"Input type {type_!r} did not return a list of records.")
        return records
    from backend.api.source_apis import (SOURCE_TYPE_SCHEMAS, fetch_custom_records,
                              fetch_source_record, is_custom_source)

    if is_custom_source(type_):
        return fetch_custom_records(config)
    if type_ not in SOURCE_TYPE_SCHEMAS:
        raise SourceError(
            f"Source node '{node.get('name')}': unknown source type "
            f"{type_!r} (expected one of {sorted(SOURCE_TYPE_SCHEMAS)})"
        )
    return [fetch_source_record(config)]


def input_sequence(graph: dict) -> dict | None:
    """The {"group", "order"} fields a wired Input type declares, if any.

    An Input whose records form trajectories (several dated observations of
    one subject) says so in its schema; the batch then runs each trajectory
    in order and stops it at its first failure. Nothing here knows the field
    names: they come from the Input type.
    """
    from .source_apis import all_source_types
    wired = {e.get("source") for e in (graph.get("edges") or [])}
    types = None
    for node in find_source_nodes(graph):
        if node.get("name") not in wired or node.get("enabled") is False:
            continue
        types = types if types is not None else all_source_types()
        sequence = (types.get((node.get("source") or {}).get("type")) or {}).get("sequence")
        if sequence and sequence.get("group"):
            return dict(sequence)
    return None


def find_source_nodes(graph: dict) -> list[dict]:
    return [t for t in graph.get("tasks", []) or [] if t.get("kind") == "source"]


# ---------------------------------------------------------------------------
# Record -> workflow inputs mapping
# ---------------------------------------------------------------------------

def map_to_workflow_inputs(records: list[dict], workflow_inputs: list[dict]) -> list[dict]:
    """Project records onto the graph's workflow inputs by name.

    Raises SourceError listing the available record fields when a required
    workflow input is not covered.
    """
    wanted = {w["name"]: w for w in workflow_inputs or []}
    mapped = []
    for i, record in enumerate(records, start=1):
        inputs = {name: record[name] for name in wanted if name in record}
        missing = [
            name for name, w in wanted.items()
            if w.get("required", True) and name not in inputs
        ]
        if missing:
            raise SourceError(
                f"Record {i}: missing required workflow inputs {missing}. "
                f"Available record fields: {sorted(record.keys(), key=str)}"
            )
        if '_dataloader' in record:
            inputs['_dataloader'] = record['_dataloader']
        mapped.append(inputs)
    return mapped
