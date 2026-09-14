"""API-backed canvas source nodes for EvoAgentX Studio.

Three source types beyond the local credit_risk feed:

- gdelt_news: GDELT doc API (artlist), free, no key. Throttling / 429 backoff
  adapted from projects/credit_risk/dataset/builders/build_r02_fetch_news.py (fair-use PAUSE).
- sec_edgar_8k: EDGAR submissions API + SGML full-text parse, adapted from
  projects/credit_risk/dataset/builders/build_r03_fetch_8k.py and build_02b_verify_events.py
  (User-Agent header is mandatory or EDGAR returns 403).
- http_api: generic JSON API with {placeholder} URL templating, optional
  params/headers/body (header values of the form "$ENV_VAR" are resolved from
  os.environ) and a dot-path `extract` into the response.

All HTTP calls: 30s timeout, up to 3 attempts with backoff on 429/5xx.
Failures raise SourceError so the run is marked failed with a visible error.
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

from backend.api.sources import SourceError

UA = {"User-Agent": "EvoAgentX Studio research (contact: research@example.com)"}
HTTP_TIMEOUT = 30
# GDELT per-IP 429s can outlive short backoffs; give 429/5xx room to clear.
RETRY_BACKOFF = [5, 15, 30, 60]  # seconds between attempts

GDELT_API = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_PAUSE = 5.0  # GDELT fair use: <= 1 request / 5 s
EDGAR_PAUSE = 0.3
EDGAR_FORMS = {"8-K", "8-K/A"}
EDGAR_TEXT_CAP = 1500  # per filing, aligned with the feed's filing_batch

_ITEM_HEADING_RE = re.compile(r"item\s+(\d\.\d\d)", re.I)

_last_gdelt_call = [0.0]
_rate_lock = threading.Lock()


# ---------------------------------------------------------------------------
# HTTP helper with retry/backoff
# ---------------------------------------------------------------------------

def _throttle_gdelt():
    """Global fair-use spacing between GDELT request starts."""
    with _rate_lock:
        wait = GDELT_PAUSE - (time.time() - _last_gdelt_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_gdelt_call[0] = time.time()


def http_request(url: str, method: str = "GET", params: dict | None = None,
                 headers: dict | None = None, body=None,
                 throttle=None) -> tuple[int, bytes]:
    """HTTP request with 30s timeout and up to 3 attempts on 429/5xx.

    throttle: optional callable invoked right before each attempt (fair use).
    Returns (status_code, response_bytes). Raises SourceError on failure.
    """
    if params:
        sep = "&" if "?" in url else "?"
        url = url + sep + urllib.parse.urlencode(params)
    data = None
    if body is not None:
        data = body.encode("utf-8") if isinstance(body, str) else body
    last_error = None
    for attempt in range(len(RETRY_BACKOFF) + 1):
        if attempt:
            time.sleep(RETRY_BACKOFF[attempt - 1])
        try:
            if throttle:
                throttle()
            req = urllib.request.Request(url, data=data, headers=headers or {},
                                         method=method.upper())
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            last_error = e
            if e.code == 429 or 500 <= e.code < 600:
                continue
            raise SourceError(f"HTTP {e.code} for {url}: {e.read()[:200]!r}")
        except Exception as e:
            last_error = e
            continue
    if isinstance(last_error, urllib.error.HTTPError) and last_error.code == 429:
        raise SourceError(
            f"Rate limited (HTTP 429) by {urllib.parse.urlparse(url).netloc} "
            f"after {len(RETRY_BACKOFF) + 1} attempts. This IP has hit the API's "
            f"fair-use cap — wait a few minutes and retry, or reduce request "
            f"frequency."
        )
    raise SourceError(f"Request to {url} failed after retries: {last_error}")


def _get_json(url: str, **kwargs) -> tuple[int, object]:
    status, raw = http_request(url, **kwargs)
    try:
        return status, json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise SourceError(f"Response from {url} is not JSON: {e}")


# ---------------------------------------------------------------------------
# gdelt_news
# ---------------------------------------------------------------------------

def fetch_gdelt_news(config: dict, start: date | None = None, end: date | None = None) -> dict:
    """One record: {company, news_batch, window_start, window_end, n_articles}.

    start/end override the `days` lookback window (watchers poll only the
    period since the last fire).
    """
    query = (config.get("query") or "").strip()
    if not query:
        raise SourceError("gdelt_news source requires a 'query'")
    days = int(config.get("days") or 30)
    max_records = min(int(config.get("max_records") or 10), 250)
    try:
        end = end or (date.fromisoformat(config['end_date']) if config.get('end_date') else datetime.now(timezone.utc).date())
        start = start or (date.fromisoformat(config['start_date']) if config.get('start_date') else end - timedelta(days=days))
    except ValueError as exc:
        raise SourceError('Use valid start/end dates (YYYY-MM-DD).') from exc
    if start > end:
        raise SourceError('Start date must be on or before end date.')

    _, data = _get_json(
        GDELT_API,
        params={
            "query": f'"{query}"',
            "mode": "artlist",
            "maxrecords": max_records,
            "format": "json",
            "sourcelang": "english",
            "startdatetime": start.strftime("%Y%m%d000000"),
            "enddatetime": end.strftime("%Y%m%d235959"),
            "sort": "datedesc",
        },
        throttle=_throttle_gdelt,
    )
    if not isinstance(data, dict) or not isinstance(data.get('articles'), list):
        raise SourceError('News API did not return an article list. Collection cannot be considered complete; retry or adjust the query.')
    articles = data['articles'][:max_records]
    lines = []
    for art in articles:
        seen = art.get("seendate", "")
        day = f"{seen[:4]}-{seen[4:6]}-{seen[6:8]}" if len(seen) >= 8 else ""
        lines.append(f"[{day}] {art.get('title', '')} — {art.get('url', '')}")
    return {
        "company": query,
        "news_batch": "\n".join(lines) or "(no news found)",
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "n_articles": len(articles),
    }


def collect_gdelt_records(config, progress=lambda done, total: None, on_record=lambda record: None):
    """Fetch every window before execution; reject capped data, never silently truncate."""
    try:
        end = date.fromisoformat(config['end_date']) if config.get('end_date') else datetime.now(timezone.utc).date()
        start = date.fromisoformat(config['start_date']) if config.get('start_date') else end - timedelta(days=int(config.get('days') or 30))
    except (ValueError, TypeError) as exc:
        raise SourceError('Use valid start/end dates (YYYY-MM-DD) and days back.') from exc
    if start > end or end > datetime.now(timezone.utc).date():
        raise SourceError('Start must be on or before end; end cannot be in the future.')
    if (end - start).days > 366:
        raise SourceError('Collect up to one year at a time.')
    step = config.get('batch_step') or 'daily'
    if step not in ('daily', 'weekly', 'monthly'):
        raise SourceError('Choose daily, weekly, or monthly batch windows.')
    windows = []
    cursor = start
    while cursor <= end:
        if step == 'monthly':
            following = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
        else:
            following = cursor + timedelta(days=1 if step == 'daily' else 7)
        last = min(end, following - timedelta(days=1))
        windows.append((cursor, last))
        cursor = last + timedelta(days=1)
    def fetch(first, last):
        record = fetch_gdelt_news({**config, 'max_records': 250}, start=first, end=last)
        if record['n_articles'] >= 250:
            if first == last:
                raise SourceError(f'News API result limit reached on {first}. Narrow the query; this collection is incomplete and cannot run.')
            mid = first + timedelta(days=(last-first).days // 2)
            return fetch(first, mid) + fetch(mid + timedelta(days=1), last)
        return [record]
    records = []
    progress(0, len(windows))
    for index, (first, last) in enumerate(windows):
        pieces = fetch(first, last)
        lines = sorted({line for piece in pieces if piece['n_articles'] for line in piece['news_batch'].splitlines()})
        if lines:
            records.append({'company': config['query'].strip(), 'news_batch': '\n'.join(lines),
                            'window_start': first.isoformat(), 'window_end': last.isoformat(),
                            'as_of': last.isoformat(), 'sample_id': config['query'].strip(),
                            'n_articles': len(lines)})
            on_record(records[-1])
        progress(index + 1, len(windows))
    return records


# ---------------------------------------------------------------------------
# sec_edgar_8k
# ---------------------------------------------------------------------------

def _html_to_text(raw: str) -> str:
    import html

    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return re.sub(r"\s+", " ", text)


def _fetch_submission_body(cik: str, adsh: str) -> str:
    """8-K body from the SGML full submission (first <DOCUMENT> of type 8-K)."""
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{adsh.replace('-', '')}"
    _, raw = http_request(f"{base}/{adsh}.txt", headers=UA)
    sgml = raw.decode("utf-8", errors="replace")
    for doc in re.findall(r"<DOCUMENT>(.*?)</DOCUMENT>", sgml, re.S):
        type_m = re.search(r"<TYPE>\s*(\S+)", doc)
        if type_m and type_m.group(1).upper().startswith("8-K"):
            text_m = re.search(r"<TEXT>(.*?)</TEXT>", doc, re.S)
            return text_m.group(1) if text_m else doc
    return sgml  # fall back to the whole submission


def fetch_edgar_8k(config: dict) -> dict:
    """One record: {cik, company, filing_batch, window_end}."""
    cik = str(config.get("cik") or "").strip().lstrip("0")
    if not cik:
        raise SourceError("sec_edgar_8k source requires a 'cik'")
    count = int(config.get("count") or 2)

    _, data = _get_json(
        f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json", headers=UA
    )
    recent = (data or {}).get("filings", {}).get("recent", {})
    filings = []
    for form, fdate, adsh in zip(recent.get("form", []),
                                 recent.get("filingDate", []),
                                 recent.get("accessionNumber", [])):
        if form in EDGAR_FORMS:
            filings.append({"filing_date": fdate, "form": form, "adsh": adsh})
    filings = filings[:count]

    blocks = []
    for f in filings:
        time.sleep(EDGAR_PAUSE)
        text = _html_to_text(_fetch_submission_body(cik, f["adsh"]))
        items = sorted(set(m.lower() for m in _ITEM_HEADING_RE.findall(text)))
        blocks.append(
            f"[{f['filing_date']}] Form {f['form']} (items: {', '.join(items)})\n"
            f"{text[:EDGAR_TEXT_CAP]}"
        )
    return {
        "cik": cik,
        "company": (data or {}).get("name", ""),
        "filing_batch": "\n\n".join(blocks) or "(no recent 8-K filings)",
        "window_end": filings[0]["filing_date"] if filings else date.today().isoformat(),
    }


# ---------------------------------------------------------------------------
# http_api (generic)
# ---------------------------------------------------------------------------

def _resolve_env(value):
    if isinstance(value, str) and value.startswith("$"):
        return os.getenv(value[1:], "")
    return value


def _extract_path(data, path: str):
    node = data
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            raise SourceError(f"extract path '{path}' not found at '{part}'")
    return node


def fetch_http_api(config: dict, *, split_records=False):
    """One record: {api_response, status_code}. {placeholders} in url/params/
    body are filled from config["vars"]."""
    url = (config.get("url") or "").strip()
    if not url:
        raise SourceError("http_api source requires a 'url'")

    def parse_json_field(name):
        raw = (config.get(name) or "").strip()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise SourceError(f"http_api source: '{name}' is not valid JSON: {e}")

    vars_ = parse_json_field("vars") or {}
    params = parse_json_field("params")
    headers = parse_json_field("headers")
    body = parse_json_field("body") if (config.get("method") or "GET").upper() == "POST" else None

    def fill(text):
        for key, value in vars_.items():
            text = text.replace("{" + str(key) + "}", urllib.parse.quote(str(value)))
        return text

    url = fill(url)
    if params:
        params = {k: fill(str(v)) for k, v in params.items()}
    if body is not None:
        body = json.dumps(body) if not isinstance(body, str) else fill(body)
    if headers:
        headers = {k: _resolve_env(v) for k, v in headers.items()}

    status, raw = http_request(
        url, method=config.get("method") or "GET", params=params,
        headers=headers, body=body,
    )
    text = raw.decode("utf-8", errors="replace")
    extract = (config.get("extract") or "").strip()
    if extract:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as e:
            raise SourceError(f"http_api source: response is not JSON: {e}")
        payload = _extract_path(payload, extract)
        text = json.dumps(payload, ensure_ascii=False)
    if split_records:
        try:
            items = json.loads(text)
        except ValueError as exc:
            raise SourceError('Batch items require a JSON array. Set Extract to the response array.') from exc
        if not isinstance(items, list):
            raise SourceError('Batch items require a JSON array. Set Extract to the response array.')
        return [{'api_response': json.dumps(item, ensure_ascii=False), 'status_code': status} for item in items]
    return {
        "api_response": text[:8000],
        "status_code": status,
    }


# ---------------------------------------------------------------------------
# Dispatch + config schema (drives the frontend Inspector form)
# ---------------------------------------------------------------------------

FETCHERS = {
    "gdelt_news": fetch_gdelt_news,
    "sec_edgar_8k": fetch_edgar_8k,
    "http_api": fetch_http_api,
}

SOURCE_TYPE_SCHEMAS = {
    "user_dataset": {
        "label": "My dataset",
        "description": "Upload and reuse your own CSV, TSV, JSON or JSONL records.",
        "config": [
            {"name": "dataset_id", "label": "Dataset", "type": "text", "required": True},
            {"name": "n", "label": "Record limit (0 = all)", "type": "number", "default": 0},
        ],
        "outputs": [],
    },
    "credit_risk": {
        "label": "Credit Risk Feed",
        "description": "Sample news + 8-K filings from projects/credit_risk/dataset/contemporary.",
        "config": [
            {"name": "split", "label": "Split", "type": "select",
             "options": ["", "train", "dev", "test"], "default": ""},
            {"name": "n", "label": "Samples per batch (0 = entire split)", "type": "number", "default": 1},
            {"name": "seed", "label": "Seed", "type": "number", "default": 42},
            {"name": "step", "label": "Walk each window", "type": "select",
             "options": ["none", "monthly", "weekly", "daily"], "default": "none"},
        ],
        "outputs": ["sample_id", "company", "symbol", "cik", "window_start",
                    "window_end", "as_of", "news_batch", "filing_batch",
                    "sample_json"],
    },
    "gdelt_news": {
        "label": "GDELT News",
        "description": "Recent English news about a query from the GDELT doc API (free, no key).",
        "config": [
            {"name": "query", "label": "Query (company / keywords)", "type": "text", "default": "", "required": True},
            {"name": "days", "label": "Days back", "type": "number", "default": 30},
            {"name": "start_date", "label": "Start date (optional, YYYY-MM-DD)", "type": "text", "default": ""},
            {"name": "end_date", "label": "End date (optional, YYYY-MM-DD)", "type": "text", "default": ""},
            {"name": "batch_step", "label": "Batch window", "type": "select", "options": ["daily", "weekly", "monthly"], "default": "daily"},
            {"name": "max_records", "label": "Max articles (single run only)", "type": "number", "default": 10},
        ],
        "outputs": ["company", "news_batch", "window_start", "window_end", "n_articles"],
    },
    "sec_edgar_8k": {
        "label": "SEC EDGAR 8-K",
        "description": "Latest 8-K filings (full text) for a CIK from SEC EDGAR (free, no key).",
        "config": [
            {"name": "cik", "label": "CIK", "type": "text", "default": "", "required": True},
            {"name": "count", "label": "Filings to fetch", "type": "number", "default": 2},
        ],
        "outputs": ["cik", "company", "filing_batch", "window_end"],
    },
    "http_api": {
        "label": "HTTP API",
        "description": "Call any JSON API. {placeholders} in url/params/body are filled from vars; header values like $ENV_VAR read from environment.",
        "config": [
            {"name": "url", "label": "URL (supports {placeholders})", "type": "text", "default": "", "required": True},
            {"name": "method", "label": "Method", "type": "select",
             "options": ["GET", "POST"], "default": "GET"},
            {"name": "vars", "label": "Vars JSON (placeholder values)", "type": "textarea", "default": "{}"},
            {"name": "params", "label": "Query params JSON", "type": "textarea", "default": ""},
            {"name": "headers", "label": "Headers JSON ($ENV_VAR allowed)", "type": "textarea", "default": ""},
            {"name": "body", "label": "Body JSON (POST)", "type": "textarea", "default": ""},
            {"name": "batch_items", "label": "Batch records", "type": "select", "options": ["response", "items"], "default": "response"},
            {"name": "extract", "label": "Extract dot-path (e.g. data.items)", "type": "text", "default": ""},
        ],
        "outputs": ["api_response", "status_code"],
    },
}


def fetch_source_record(config: dict) -> dict:
    """Execute one API-backed source node config; returns a single record."""
    type_ = (config or {}).get("type")
    fetcher = FETCHERS.get(type_)
    if fetcher is None:
        raise SourceError(f"Unknown API source type: {type_!r}")
    return fetcher(config)


def all_source_types() -> dict:
    """Every source type the canvas may use: the built-in feeds and APIs,
    plus whatever custom toolkits have offered as input sources."""
    from backend.api import custom_tools

    import copy
    from backend.api.datasets import catalog
    schemas = copy.deepcopy(SOURCE_TYPE_SCHEMAS)
    from backend.api.sources import CREDIT_RISK_SAMPLES
    available = (['contemporary'] if CREDIT_RISK_SAMPLES.is_file() else []) + [item['id'] for item in catalog()]
    schemas['credit_risk']['config'].insert(0, {'name': 'dataset', 'label': 'Dataset version', 'type': 'select',
        'default': available[0] if available else '', 'options': available})
    schemas['credit_risk']['description'] = 'Choose a dataset version and split. Releases emit dated observations in trajectory order.'
    return {**schemas, **custom_tools.custom_source_types()}


def is_custom_source(type_) -> bool:
    return isinstance(type_, str) and type_.startswith("custom:")


def fetch_custom_records(config: dict) -> list[dict]:
    """Run a custom tool as a source and return its records.

    One dict is one record; a list of dicts is several. Anything else is not
    a source, and says so rather than feeding the workflow a string.
    """
    from backend.api import tools_registry

    tool = config["type"][len("custom:"):]
    schema = all_source_types().get(config["type"])
    if schema is None:
        raise SourceError(f"Unknown custom source {config['type']!r}: is the toolkit "
                          "still saved, and the function still marked as a source?")
    args = {f["name"]: config.get(f["name"]) for f in schema["config"]
            if config.get(f["name"]) not in (None, "")}
    result = tools_registry.call_tool(tool, args)
    if isinstance(result, dict):
        return [result]
    if isinstance(result, list) and all(isinstance(r, dict) for r in result):
        return result
    raise SourceError(f"Source tool '{tool}' returned a {type(result).__name__}; an input "
                      "source must return a record (dict) or a list of records.")
