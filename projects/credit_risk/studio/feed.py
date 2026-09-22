"""The credit-risk dataset as a Studio Input type.

Two kinds of data:
- the legacy contemporary samples (dataset/contemporary/samples.jsonl), one
  company window per sample, optionally stepped through its window;
- versioned releases (dataset/expansion/releases/<id>/), dated observations
  in trajectory order.

Each record carries `sample_id` (the trajectory) and `as_of` (the date its
evidence runs up to); the plugin declares them as the Input's sequence, so
Studio runs a trajectory in date order without knowing either name.
"""

import json
import random
from pathlib import Path

from backend.features.data.sources import SourceError

_PROJECT = Path(__file__).resolve().parents[1]

CREDIT_RISK_SAMPLES = _PROJECT / "dataset" / "contemporary" / "samples.jsonl"

CREDIT_RISK_FIELDS = [
    "sample_id", "company", "symbol", "cik",
    "window_start", "window_end", "as_of", "news_batch", "filing_batch",
    "sample_json", "flags",
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
        # The dataset's own verdict on its news stream (R06 news_quality):
        # "generic_name,noisy" on a sample whose headlines are not about the
        # company. Travels with the record so a batch can be read with and
        # without such samples; the pipeline itself never looks at it.
        "flags": ",".join((sample.get("news_quality") or {}).get("flags") or []),
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


def credit_risk_info(dataset=None) -> dict:
    from . import releases
    versions = releases.catalog()
    legacy = [{"id": "contemporary", "label": "Contemporary (legacy)"}] if CREDIT_RISK_SAMPLES.is_file() else []
    available = [*legacy, *versions]
    if not legacy and versions and dataset in (None, 'contemporary'):
        dataset = versions[0]['id']
    if dataset and dataset != "contemporary":
        selected = next((item for item in versions if item["id"] == dataset), None)
        if selected is None:
            raise SourceError("Unknown dataset version")
        return {**selected, "dataset": dataset, "fields": CREDIT_RISK_FIELDS, "datasets": available}
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
        "dataset": "contemporary",
        "datasets": [{**item, "splits": splits} for item in legacy] + versions,
        "splits": splits,
        "fields": CREDIT_RISK_FIELDS,
    }


def credit_risk_records(split: str | None = None, n: int = 5, seed: int = 42,
                        with_labels: bool = False, step: str | None = None, dataset: str | None = None) -> list[dict]:
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
    if dataset and dataset != 'contemporary':
        from .releases import records
        return records(dataset, split=split, n=n, seed=seed, with_labels=with_labels, step=step)
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
