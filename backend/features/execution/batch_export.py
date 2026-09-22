"""Getting a batch's results out of the Studio.

An evaluation is only half-useful while its numbers live inside the tool that
produced them: the reason to score 200 records is to hand someone the result.
CSV is what a spreadsheet and a colleague both accept; JSON keeps the structure
for anything downstream.
"""

import csv
import io
import json

# Fixed leading columns, in the order someone reading the sheet wants them:
# what went in, what came out, how it scored.
_BASE_COLUMNS = ["index", "status", "score", "label", "output", "error", "run_id"]


def _flat(value) -> str:
    """One cell. Structured values are JSON, not Python reprs."""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _input_columns(items: list[dict]) -> list[str]:
    """Every input field any record had, in first-seen order.

    Records are not guaranteed to share a shape — a preprocessor may add a
    field, a source may omit one — so the union is taken rather than the first
    record's keys, and a record missing a column gets an empty cell.
    """
    columns: list[str] = []
    for item in items:
        for key in (item.get("inputs") or {}):
            if key not in columns:
                columns.append(key)
    return columns


def _score_columns(items: list[dict]) -> list[str]:
    """Extra fields a custom metric returned alongside its score."""
    columns: list[str] = []
    for item in items:
        detail = item.get("score_detail") or {}
        if not isinstance(detail, dict):
            continue
        for key in detail:
            name = f"score_{key}"
            if name not in columns:
                columns.append(name)
    return columns


def _row(item: dict, input_columns: list[str], score_columns: list[str]) -> dict:
    inputs = item.get("inputs") or {}
    detail = item.get("score_detail") or {}
    row = {
        "index": item.get("index"),
        "status": item.get("status"),
        "score": item.get("score"),
        "label": item.get("label"),
        "output": item.get("output_summary"),
        "error": item.get("error"),
        "run_id": item.get("run_id"),
    }
    for key in input_columns:
        row[f"input_{key}"] = inputs.get(key)
    for name in score_columns:
        row[name] = detail.get(name[len("score_"):]) if isinstance(detail, dict) else None
    return row


def to_rows(batch: dict) -> tuple[list[str], list[dict]]:
    """Column names and one row per record."""
    from .batch import BATCHES_DIR
    items = [{**item, 'inputs':json.loads((BATCHES_DIR / item['input_file']).read_text())} if item.get('input_file') else item
             for item in batch.get('items') or []]
    input_columns = _input_columns(items)
    score_columns = _score_columns(items)
    columns = (_BASE_COLUMNS[:4]
               + [f"input_{k}" for k in input_columns]
               + _BASE_COLUMNS[4:]
               + score_columns)
    return columns, [_row(i, input_columns, score_columns) for i in items]


def to_csv(batch: dict) -> str:
    columns, rows = to_rows(batch)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: _flat(row.get(k)) for k in columns})
    return buffer.getvalue()


def to_json(batch: dict) -> str:
    """The records plus the context needed to read them later.

    Which metric produced these scores, and how much of the set they cover, is
    part of the result — a bare list of numbers is not interpretable once it
    has left the tool.
    """
    _, rows = to_rows(batch)
    document = {
        "batch_id": batch.get("batch_id"),
        "graph_id": batch.get("graph_id"),
        "status": batch.get("status"),
        "created_at": batch.get("created_at"),
        "source": batch.get("source"),
        "metric": batch.get("metric"),
        "summary": batch.get("summary"),
        "total": batch.get("total"),
        "results": rows,
    }
    return json.dumps(document, indent=2, ensure_ascii=False, default=str)


def filename(batch: dict, suffix: str) -> str:
    graph = batch.get("graph_id") or "workflow"
    return f"{graph}-batch-{batch.get('batch_id')}.{suffix}"
