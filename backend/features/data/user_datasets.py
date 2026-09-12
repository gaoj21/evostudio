"""Reusable, user-owned input datasets. No model calls or domain assumptions."""
import csv
import io
import json
import re
import threading
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Body, HTTPException, Request
from backend.api.studio_config import data_path
from backend.api.sources import SourceError

router = APIRouter(prefix="/api/datasets", tags=["datasets"])
MAX_BYTES = 20 * 1024 * 1024
MAX_ROWS = 50000
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


def public(item, preview=False):
    result = {k: v for k, v in item.items() if k != "records"}
    if preview:
        # Keep the preview small even if one row contains an entire document.
        result["preview"] = [{k: (str(v)[:500] + "…" if len(str(v)) > 500 else v)
                              for k, v in row.items()} for row in item["records"][:5]]
    return result


def parse(filename, content):
    if len(content) > MAX_BYTES:
        raise SourceError("Dataset exceeds 20 MB. Split the file before uploading; nothing was truncated.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SourceError("Export the file as UTF-8 and try again.") from None
    if "\x00" in text:
        raise SourceError("Binary file detected. Export as CSV, TSV, JSON or JSONL.")
    ext = filename.lower().rsplit(".", 1)[-1]
    try:
        if ext in ("csv", "tsv"):
            reader = csv.DictReader(io.StringIO(text), delimiter="\t" if ext == "tsv" else ",")
            headers = reader.fieldnames or []
            if not headers or any(not h.strip() for h in headers) or len(headers) != len(set(headers)):
                raise SourceError("Use a header row with non-empty, unique column names.")
            records = []
            for i, row in enumerate(reader, 2):
                if None in row or any(v is None for v in row.values()):
                    raise SourceError(f"Row {i}: column count does not match the header.")
                records.append(row)
                if len(records) > MAX_ROWS:
                    raise SourceError("Dataset exceeds 50,000 records. Split the file first.")
        elif ext == "json":
            records = json.loads(text)
            if isinstance(records, dict) and isinstance(records.get("records"), list):
                records = records["records"]
        elif ext == "jsonl":
            records = []
            for i, line in enumerate(text.splitlines(), 1):
                if line.strip():
                    try:
                        records.append(json.loads(line))
                    except ValueError:
                        raise SourceError(f"JSONL line {i}: invalid JSON.") from None
        else:
            raise SourceError("Choose a CSV, TSV, JSON or JSONL file. Export Excel sheets as CSV first.")
        if not isinstance(records, list) or not records:
            raise SourceError('Provide a non-empty array of objects, JSONL objects, or a CSV/TSV table.')
        if len(records) > MAX_ROWS:
            raise SourceError("Dataset exceeds 50,000 records. Split the file first.")
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
    mapped = [{target: row.get(source) for source, target in mapping.items()} for row in rows]
    mode = config.get("input_mode", "records")
    if mode == "reference":
        field = config.get("reference_field", "obligor_list")
        if not isinstance(field, str) or not field.strip():
            raise SourceError("Give the shared reference a non-empty output name.")
        return [{field: mapped}]
    if mode != "records":
        raise SourceError("Choose per-record input or shared reference.")
    return mapped


@router.get("")
def list_datasets():
    items = [public(json.loads(p.read_text(encoding="utf-8"))) for p in directory().glob("*.json")]
    return {"datasets": sorted(items, key=lambda r: r["created_at"], reverse=True)}


@router.post("")
async def upload_dataset(request: Request):
    form = await request.form()
    file = form.get("file")
    if not hasattr(file, "read"):
        raise HTTPException(422, "Choose a dataset file.")
    try:
        content = await file.read(MAX_BYTES + 1)
        filename = (file.filename or "dataset").replace("\\", "/").rsplit("/", 1)[-1]
        rows, fields = parse(filename, content)
        name = str(form.get("name") or filename).strip()
        if not name or len(name) > 120:
            raise SourceError("Dataset name must be between 1 and 120 characters.")
        item = {"id": uuid.uuid4().hex, "name": name, "filename": filename,
                "created_at": datetime.now(timezone.utc).isoformat(), "size_bytes": len(content),
                "row_count": len(rows), "fields": fields, "records": rows}
        save(item)
        return public(item, preview=True)
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
            path_for(dataset_id).unlink()
        return {"deleted": dataset_id}
    except SourceError as exc:
        raise HTTPException(404, str(exc)) from exc
