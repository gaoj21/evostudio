"""Batch input sources for EvoAgentX Studio.

Two sources turn records into workflow inputs:
- Uploaded JSONL (one JSON object per line) / CSV (column names -> input names).
- The credit_risk feed simulator, sampling credit_risk/dataset/contemporary/
  samples.jsonl and projecting each sample onto a canonical field set.

Mapping rule (shared): a record fills a workflow input when the names match;
unmatched required inputs are a hard error, unmatched record fields ignored.
"""

import csv
import io
import json
import random
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

CREDIT_RISK_SAMPLES = _REPO_ROOT / "credit_risk" / "dataset" / "contemporary" / "samples.jsonl"

CREDIT_RISK_FIELDS = [
    "sample_id", "company", "symbol", "cik",
    "window_start", "window_end", "as_of", "news_batch", "filing_batch",
    "sample_json",
]

# A sample is one obligor over one window, and its window holds tens of dated
# news items (median 78, up to 773). Fed as one blob they are all seen at once:
# July's signal and December's arrive together, nothing accumulates, and the
# pipeline's own "is this a duplicate of something we already alerted on"
# reasoning has nothing to compare against.
#
# Stepping through the window instead turns one sample into a sequence of
# runs, each seeing only what had arrived by its own `as_of` date. Memory then
# has a timeline to be about.
STEP_CHOICES = ("none", "daily", "weekly", "monthly")
_STEP_DAYS = {"daily": 1, "weekly": 7, "monthly": 30}

_NEWS_MAX_ITEMS = 20
_NEWS_SNIPPET_CHARS = 300
_NEWS_TOTAL_CHARS = 8000
_FILING_MAX_ITEMS = 5
_FILING_SNIPPET_CHARS = 1500
_FILING_TOTAL_CHARS = 6000


class SourceError(Exception):
    """User-facing source/mapping error (returned as HTTP 422)."""


# ---------------------------------------------------------------------------
# credit_risk feed simulator
# ---------------------------------------------------------------------------

def _news_batch(sample: dict) -> str:
    """The window's news, most recent first.

    Taking the first N of a date-sorted list handed a six-month window's worth
    of monitoring the first three days of it and threw away everything since —
    on a pipeline whose job is to notice distress building towards the end.
    """
    lines = []
    total = 0
    ordered = sorted(sample.get("news") or [], key=_item_date, reverse=True)
    for item in ordered[:_NEWS_MAX_ITEMS]:
        snippet = (item.get("text") or "").replace("\n", " ")[:_NEWS_SNIPPET_CHARS]
        line = f"[{item.get('date', '')}] {item.get('title', '')} — {snippet}"
        if total + len(line) > _NEWS_TOTAL_CHARS:
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


def _filing_batch(sample: dict) -> str:
    blocks = []
    total = 0
    ordered = sorted(sample.get("filings") or [],
                     key=lambda f: str(f.get("filing_date") or ""), reverse=True)
    for filing in ordered[:_FILING_MAX_ITEMS]:
        items = ", ".join(filing.get("items") or [])
        snippet = (filing.get("text") or "").replace("\n", " ")[:_FILING_SNIPPET_CHARS]
        block = (
            f"[{filing.get('filing_date', '')}] Form {filing.get('form', '')}"
            f" (items: {items})\n{snippet}"
        )
        if total + len(block) > _FILING_TOTAL_CHARS:
            break
        blocks.append(block)
        total += len(block)
    return "\n\n".join(blocks) or "(no 8-K filings in window)"


def _sample_to_record(sample: dict) -> dict:
    company = sample.get("company") or {}
    window = sample.get("window") or {}
    return {
        "sample_id": sample.get("sample_id", ""),
        "company": company.get("query_name") or company.get("name", ""),
        "symbol": company.get("symbol", ""),
        "cik": company.get("cik", ""),
        "window_start": window.get("start", ""),
        "window_end": window.get("end", ""),
        "as_of": window.get("end", ""),
        "news_batch": _news_batch(sample),
        "filing_batch": _filing_batch(sample),
        "sample_json": json.dumps(sample, ensure_ascii=False),
    }


def _item_date(item: dict) -> str:
    return str(item.get("date") or item.get("seendate") or "")[:10]


def step_sample(sample: dict, step: str) -> list[dict]:
    """One sample as a sequence of records, one per step of its window.

    Each record carries the news and filings dated up to and including its own
    `as_of`, so a later run sees everything an earlier one did plus what has
    since arrived — which is how the pipeline would meet them in real life.

    Steps with nothing new are skipped: a run that sees exactly what the last
    one saw has nothing to add and costs a round of LLM calls.
    """
    from datetime import date, timedelta

    days = _STEP_DAYS.get(step)
    window = sample.get("window") or {}
    start, end = str(window.get("start") or "")[:10], str(window.get("end") or "")[:10]
    if not days or not (start and end):
        return [_sample_to_record(sample)]

    try:
        cursor, last = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        return [_sample_to_record(sample)]

    news = sorted(sample.get("news") or [], key=_item_date)
    filings = sorted(sample.get("filings") or [], key=lambda f: str(f.get("filing_date") or ""))

    # Counted back from the window's close, so the last observation is always
    # the window's end however the step divides into it — rather than landing
    # a day short and leaving the window's own end unrepresented.
    marks = []
    while cursor <= last:
        marks.append(last.isoformat())
        last -= timedelta(days=days)
    marks.reverse()

    records, seen = [], -1
    for as_of in marks:
        upto_news = [i for i in news if _item_date(i) <= as_of]
        upto_filings = [f for f in filings if str(f.get("filing_date") or "")[:10] <= as_of]
        total = len(upto_news) + len(upto_filings)
        if total and total > seen:
            seen = total
            records.append({
                **_sample_to_record({**sample, "news": upto_news, "filings": upto_filings}),
                "as_of": as_of,
            })
    return records


def credit_risk_info() -> dict:
    splits: dict[str, int] = {}
    if CREDIT_RISK_SAMPLES.is_file():
        with open(CREDIT_RISK_SAMPLES, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                split = json.loads(line).get("split", "")
                splits[split] = splits.get(split, 0) + 1
    return {
        "id": "credit_risk",
        "label": "credit_risk feed (contemporary/samples.jsonl)",
        "splits": splits,
        "fields": CREDIT_RISK_FIELDS,
    }


def credit_risk_records(split: str | None = None, n: int = 5, seed: int = 42,
                        with_labels: bool = False, step: str | None = None) -> list[dict]:
    """Sample the credit_risk dataset.

    n <= 0 means "the entire split" (file order, no sampling) — used for
    full-dataset batch runs.

    step ("daily"/"weekly"/"monthly") walks each window instead of handing it
    over whole, so one sample becomes a sequence of runs and the pipeline meets
    its news the way it would arrive.

    with_labels=False: list of input-field dicts (for batch runs).
    with_labels=True: list of {"inputs": {...}, "label": {...}, "id": ...}
    evaluation records (for evolve).
    """
    if not CREDIT_RISK_SAMPLES.is_file():
        raise SourceError(f"credit_risk dataset not found at {CREDIT_RISK_SAMPLES}")
    samples = []
    with open(CREDIT_RISK_SAMPLES, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            sample = json.loads(line)
            if split and sample.get("split") != split:
                continue
            samples.append(sample)
    if not samples:
        raise SourceError(f"No credit_risk samples found for split '{split}'")
    if n and n > 0:
        rng = random.Random(seed)
        rng.shuffle(samples)
        samples = samples[:n]
    if not with_labels:
        if step and step != "none":
            return [record for sample in samples for record in step_sample(sample, step)]
        return [_sample_to_record(s) for s in samples]
    return [
        {
            "inputs": _sample_to_record(s),
            "label": {"type": s.get("type"), "event": (s.get("label") or {}).get("event")},
            "id": s.get("sample_id", f"sample-{i + 1}"),
        }
        for i, s in enumerate(samples)
    ]


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
    text = content.decode("utf-8-sig")
    name = (filename or "").lower()
    if name.endswith(".jsonl"):
        records = []
        for i, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise SourceError(f"JSONL line {i}: invalid JSON ({e})")
            if not isinstance(obj, dict):
                raise SourceError(f"JSONL line {i}: expected a JSON object")
            records.append(obj)
    elif name.endswith(".csv"):
        records = [dict(row) for row in csv.DictReader(io.StringIO(text))]
    else:
        raise SourceError(f"Unsupported file type for '{filename}'; use .jsonl or .csv")
    if not records:
        raise SourceError("Uploaded file contains no records")
    return records


# ---------------------------------------------------------------------------
# Canvas source nodes
# ---------------------------------------------------------------------------

def records_from_source_node(node: dict) -> list[dict]:
    """Execute a canvas source node (kind="source"); returns n records.

    type "credit_risk" samples the local dataset; other types dispatch to the
    API fetchers in source_apis (single record per execution).
    """
    config = node.get("source") or {}
    type_ = config.get("type")
    if type_ == "credit_risk":
        n_raw = config.get("n")
        return credit_risk_records(
            split=config.get("split") or None,
            n=int(n_raw) if n_raw is not None else 1,  # n=0: entire split
            seed=int(config.get("seed") or 42),
            step=config.get("step"),
        )
    from .source_apis import SOURCE_TYPE_SCHEMAS, fetch_source_record

    if type_ not in SOURCE_TYPE_SCHEMAS:
        raise SourceError(
            f"Source node '{node.get('name')}': unknown source type "
            f"{type_!r} (expected one of {sorted(SOURCE_TYPE_SCHEMAS)})"
        )
    return [fetch_source_record(config)]


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
                f"Available record fields: {sorted(record.keys())}"
            )
        mapped.append(inputs)
    return mapped
