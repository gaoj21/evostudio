"""Reusable, user-owned input datasets. No model calls or domain assumptions."""
import csv
import io
import json
import os
import sys
import re
import threading
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Body, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from backend.api.studio_config import data_path
from backend.api.sources import SourceError

router = APIRouter(prefix="/api/datasets", tags=["datasets"])
# Zero means no application-level cap; deployments may opt into limits.
MAX_BYTES = max(0, int(os.getenv('EVO_DATASET_MAX_BYTES', '0')))
MAX_ROWS = max(0, int(os.getenv('EVO_DATASET_MAX_ROWS', '0')))
csv.field_size_limit(sys.maxsize)
_lock = threading.RLock()


def directory():
    path = data_path("datasets")
    path.mkdir(parents=True, exist_ok=True)
    return path


def path_for(dataset_id):
    if not isinstance(dataset_id, str) or not re.fullmatch(r"[a-f0-9]{32}", dataset_id):
        raise SourceError("Invalid dataset ID.")
    return directory() / f"{dataset_id}.json"


def load(dataset_id):
    path = path_for(dataset_id)
    if not path.is_file():
        raise SourceError("Dataset no longer exists. Choose or upload a dataset in Input.")
    return json.loads(path.read_text(encoding="utf-8"))


def save(item):
    path = path_for(item["id"])
    temporary = path.with_suffix(".tmp")
    with _lock:
        temporary.write_text(json.dumps(item, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        temporary.replace(path)


def field_types(item):
    def kind(value):
        if isinstance(value, bool): return 'bool'
        if isinstance(value, int): return 'int'
        if isinstance(value, float): return 'float'
        if isinstance(value, list): return 'list'
        if isinstance(value, dict): return 'dict'
        return 'str'
    result = {}
    for field in item['fields']:
        kinds = {kind(row[field]) for row in item['records'] if row.get(field) is not None}
        result[field] = next(iter(kinds)) if len(kinds) == 1 else 'float' if kinds and kinds <= {'int', 'float'} else 'any'
    return result


def _mentions(config, dataset_id):
    """A source config uses the dataset directly or through a wrapper
    (a DataLoader's source_config, or any other nested adapter config)."""
    if isinstance(config, dict):
        return config.get('dataset_id') == dataset_id or any(_mentions(v, dataset_id) for v in config.values())
    if isinstance(config, list):
        return any(_mentions(v, dataset_id) for v in config)
    return False


def references(dataset_id):
    from backend.api import graphs
    uses = []
    for path in graphs.GRAPHS_DIR.glob('*.json'):
        graph = json.loads(path.read_text(encoding='utf-8'))
        for node in graph.get('tasks', []):
            if _mentions(node.get('source') or {}, dataset_id):
                uses.append({'graph_id': graph['id'], 'graph_name': graph.get('name', graph['id']), 'node': node['name']})
    return uses


def public(item, preview=False):
    result = {k: v for k, v in item.items() if k != "records"}
    result['field_types'] = field_types(item)
    if preview:
        result['used_by'] = references(item['id'])
        # Keep the preview small even if one row contains an entire document.
        result["preview"] = [{k: (str(v)[:500] + "…" if len(str(v)) > 500 else v)
                              for k, v in row.items()} for row in item["records"][:5]]
    return result


def parse(filename, content):
    if MAX_BYTES and len(content) > MAX_BYTES:
        raise SourceError("Dataset exceeds the configured upload size limit; nothing was truncated.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SourceError("Export the file as UTF-8 and try again.") from None
    if "\x00" in text:
        raise SourceError("Binary file detected. Export as CSV, TSV, JSON or JSONL.")
    ext = filename.lower().rsplit(".", 1)[-1]
    try:
        if ext in ("csv", "tsv"):
            # Third-party tool imports may reset this process-global setting.
            csv.field_size_limit(sys.maxsize)
            # TSV has no quoting: a cell starting with '"' is data, not the
            # start of a quoted field that swallows the rows after it.
            reader = (csv.DictReader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE)
                      if ext == "tsv" else csv.DictReader(io.StringIO(text)))
            headers = reader.fieldnames or []
            if not headers or any(not h.strip() for h in headers) or len(headers) != len(set(headers)):
                raise SourceError("Use a header row with non-empty, unique column names.")
            records = []
            for i, row in enumerate(reader, 2):
                if None in row or any(v is None for v in row.values()):
                    raise SourceError(f"Row {i}: column count does not match the header.")
                records.append(row)
                if MAX_ROWS and len(records) > MAX_ROWS:
                    raise SourceError("Dataset exceeds the configured row limit; nothing was truncated.")
        elif ext in ('json', 'jsonl'):
            from .json_records import parse_json_records
            records = parse_json_records(text, MAX_ROWS)
        else:
            raise SourceError("Choose a CSV, TSV, JSON or JSONL file. Export Excel sheets as CSV first.")
        if not isinstance(records, list) or not records:
            raise SourceError('Provide a non-empty array of objects, JSONL objects, or a CSV/TSV table.')
        if MAX_ROWS and len(records) > MAX_ROWS:
            raise SourceError("Dataset exceeds the configured row limit; nothing was truncated.")
        for i, row in enumerate(records, 1):
            if not isinstance(row, dict) or not row or any(not k.strip() for k in row):
                raise SourceError(f"Record {i}: expected an object with non-empty field names.")
        # Reject JSON NaN/Infinity instead of persisting unreadable API responses.
        json.dumps(records, allow_nan=False)
    except (ValueError, csv.Error) as exc:
        raise SourceError(f"Invalid dataset: {exc}") from None
    fields = list(dict.fromkeys(k for row in records for k in row))
    if len(fields) > 200:
        raise SourceError("Dataset has more than 200 fields. Select the relevant columns first.")
    return records, fields


def convert_value(value, kind):
    if value is None or (value == '' and kind != 'str'):
        return None
    if kind == 'str': return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if kind == 'any': return value
    if kind == 'bool':
        if isinstance(value, bool): return value
        if isinstance(value, str) and value.lower() in ('true', 'false'): return value.lower() == 'true'
        raise ValueError('expected true or false')
    if kind in ('int', 'float'):
        import math
        if isinstance(value, bool): raise ValueError('boolean is not a number')
        number = float(value)
        if not math.isfinite(number): raise ValueError('expected a finite number')
        if kind == 'int':
            if isinstance(value, int): return value
            if isinstance(value, str) and re.fullmatch(r'[+-]?\d+', value.strip()): return int(value)
            if not number.is_integer(): raise ValueError('expected a whole number')
            return int(number)
        return number
    if kind in ('list', 'dict'):
        result = json.loads(value) if isinstance(value, str) else value
        if not isinstance(result, list if kind == 'list' else dict): raise ValueError('unexpected JSON type')
        return result
    raise ValueError('unknown column type')


def records(config):
    item = load(config.get("dataset_id"))
    mapping = config.get("field_mapping") or {f: f for f in item["fields"]}
    if not isinstance(mapping, dict) or any(k not in item["fields"] for k in mapping):
        raise SourceError("Dataset field mapping references an unknown column.")
    targets = list(mapping.values())
    if any(not isinstance(v, str) or not v.strip() for v in targets) or len(targets) != len(set(targets)):
        raise SourceError("Output field names must be non-empty and unique.")
    try:
        limit = int(config.get("n") or 0)
    except (ValueError, TypeError):
        raise SourceError("Record limit must be an integer.") from None
    if limit < 0:
        raise SourceError("Record limit cannot be negative. Use 0 for all records.")
    rows = item["records"][:limit] if limit else item["records"]
    overrides = config.get('column_types') or {}
    if not isinstance(overrides, dict) or any(key not in item['fields'] or kind not in ('str', 'int', 'float', 'bool', 'list', 'dict', 'any') for key, kind in overrides.items()):
        raise SourceError('Column types must refer to existing fields and supported types.')
    mapped = []
    for index, row in enumerate(rows, 1):
        out = {}
        for source, target in mapping.items():
            try:
                out[target] = convert_value(row.get(source), overrides[source]) if source in overrides else row.get(source)
            except (ValueError, TypeError, OverflowError) as exc:
                raise SourceError(f"Record {index}, column '{source}': cannot convert to {overrides[source]} ({exc}).") from None
        mapped.append(out)
    mode = config.get("input_mode", "records")
    if mode == "reference":
        field = config.get("reference_field") or "reference"
        if not isinstance(field, str) or not field.strip():
            raise SourceError("Give the shared reference a non-empty output name.")
        return [{field: mapped}]
    if mode != "records":
        raise SourceError("Choose per-record input or shared reference.")
    return mapped


# Listing summarises every dataset, and a summary needs the records (their
# field types). Reading and typing every record of every dataset on every
# listing is what made the picker slow once a dataset held real data; the
# summary is kept per file version instead (a write changes size or mtime).
_summaries: dict = {}


def summary(path):
    state = path.stat()
    key = (str(path), state.st_size, state.st_mtime_ns)
    if key not in _summaries:
        if len(_summaries) > 512:
            _summaries.clear()
        _summaries[key] = public(json.loads(path.read_text(encoding="utf-8")))
    return _summaries[key]


@router.get("")
def list_datasets():
    items = [summary(path) for path in directory().glob("*.json")]
    return {"datasets": sorted(items, key=lambda r: r["created_at"], reverse=True)}


@router.post("")
async def upload_dataset(request: Request):
    form = await request.form()
    file = form.get("file")
    if not hasattr(file, "read"):
        raise HTTPException(422, "Choose a dataset file.")
    try:
        content = await file.read(MAX_BYTES + 1 if MAX_BYTES else -1)
        filename = (file.filename or "dataset").replace("\\", "/").rsplit("/", 1)[-1]
        rows, fields = await run_in_threadpool(parse, filename, content)
        name = str(form.get("name") or filename).strip()
        if not name or len(name) > 120:
            raise SourceError("Dataset name must be between 1 and 120 characters.")
        item = {"id": uuid.uuid4().hex, "name": name, "filename": filename,
                "created_at": datetime.now(timezone.utc).isoformat(), "size_bytes": len(content),
                "row_count": len(rows), "fields": fields, "records": rows}
        await run_in_threadpool(save, item)
        return await run_in_threadpool(public, item, True)
    except SourceError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        await file.close()


@router.get("/{dataset_id}")
def get_dataset(dataset_id: str):
    try:
        return public(load(dataset_id), preview=True)
    except SourceError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.patch("/{dataset_id}")
def rename_dataset(dataset_id: str, body: dict = Body(...)):
    name = body.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
        raise HTTPException(422, "Dataset name must be between 1 and 120 characters.")
    try:
        with _lock:
            item = load(dataset_id)
            item["name"] = name.strip()
            save(item)
        return public(item, preview=True)
    except SourceError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.delete("/{dataset_id}")
def delete_dataset(dataset_id: str):
    try:
        with _lock:
            load(dataset_id)
            used_by = references(dataset_id)
            if used_by:
                names = ', '.join(f"{r['graph_name']} / {r['node']}" for r in used_by)
                raise HTTPException(409, f'Dataset is in use by: {names}. Detach it from these Inputs and save the workflows before deleting.')
            path_for(dataset_id).unlink()
        return {"deleted": dataset_id}
    except SourceError as exc:
        raise HTTPException(404, str(exc)) from exc
