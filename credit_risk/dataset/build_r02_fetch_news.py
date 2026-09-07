#!/usr/bin/env python
"""Step R02: fetch last-year news for contemporary-dataset companies via GDELT.

FNSPID ends in 2023, so the contemporary dataset uses the GDELT DOC 2.0 API
(free, 2017–present, title + URL + domain — headlines only, like FNSPID).

Input: contemporary/candidates.csv with columns
    type(positive|negative), cik, name, query_name, symbol, ref_date
where ref_date = event_date (positives) or the sampled window end
(negatives). Window = [ref_date - 180d, ref_date - 1d], same as v0.1+.

Per company:
- query GDELT artlist with the quoted query_name, month by month
  (auto-splits a month when it hits the 250-record cap)
- keep English articles whose TITLE mentions the company (GDELT matches on
  body text too — unchecked hits are mostly noise, see R2 gate in v0.1)
- dedup on (date, normalized title)
- density gate: >= MIN_NEWS rows in the window, else the company is dropped

Rate limiting: GDELT asks for <= 1 request / 5 s; 429s get 30 s backoff.
Requests are throttled globally but companies are fetched by a small thread
pool (GDELT server-side latency, not the rate limit, is the bottleneck).

Output: contemporary/raw/news/<type>_<key>.jsonl (one article per line:
date, title, url, publisher) and a printed per-company summary.
Companies below the density gate leave a `.dropped` marker so restarts skip
them; ERRORs leave no marker and are retried on the next run.

Run from the repository root:
    .venv/bin/python credit_risk/dataset/build_r02_fetch_news.py
"""

import csv
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

BASE = "credit_risk/dataset/contemporary"
CANDIDATES = os.path.join(BASE, "candidates.csv")
OUT_DIR = os.path.join(BASE, "raw", "news")

WINDOW_DAYS = 180
MIN_NEWS = 20
MAXRECORDS = 250
PAUSE = 8.0           # GDELT fair-use: keep request starts well spaced
BACKOFF = [30, 60, 120, 240, 300]  # ~12 min patience; fail fast, retry later
TIMEOUT = 150         # GDELT often answers in 30-60 s under load
WORKERS = 3           # >3 concurrent connections triggers GDELT 429s
MAX_REQUESTS = 60     # per-company budget; beyond it keep partial coverage

API = "https://api.gdeltproject.org/api/v2/doc/doc"


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", s.lower()).strip()


NAME_STOP = {"inc", "corp", "corporation", "group", "co", "company", "ltd",
             "llc", "lp", "holdings", "holding", "the", "plc", "sa", "nv",
             "trust", "partners", "acquisition", "incorporated", "usa",
             "u", "s", "us"}


def name_tokens(query_name: str):
    toks = [t for t in norm(query_name).split() if t not in NAME_STOP]
    return toks


def title_mentions(title: str, tokens) -> bool:
    """R2-style gate: all (multi-token) key tokens appear in the title."""
    if not tokens:
        return False
    nt = " " + norm(title) + " "
    hits = sum(1 for t in tokens if f" {t} " in nt)
    if len(tokens) == 1:
        return hits == 1 and len(tokens[0]) >= 3
    return hits == len(tokens)


_last_call = [0.0]
_rate_lock = threading.Lock()


def _throttle():
    """Global fair-use spacing: >= PAUSE between request starts."""
    with _rate_lock:
        wait = PAUSE - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()


def gdelt(query: str, start: date, end: date, counter: dict) -> list:
    """All English artlist records for [start, end]; splits on the cap."""
    params = urllib.parse.urlencode({
        "query": f'"{query}"',
        "mode": "artlist",
        "maxrecords": MAXRECORDS,
        "format": "json",
        "sourcelang": "english",
        "startdatetime": start.strftime("%Y%m%d000000"),
        "enddatetime": end.strftime("%Y%m%d235959"),
        "sort": "datedesc",
    })
    for attempt, backoff in enumerate([0] + BACKOFF):
        if backoff:
            time.sleep(backoff)
        try:
            _throttle()
            counter["n"] += 1
            req = urllib.request.Request(API + "?" + params)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except Exception as e:
            status = getattr(e, "code", None)
            if status == 429 or status is None or 500 <= (status or 0) < 600:
                if attempt == len(BACKOFF):
                    raise
                continue
            raise
    arts = data.get("articles", [])
    # Split saturated windows, but bottom out at ~7 days: a company with
    # >250 articles/week (Tesla, Nikola, ...) would otherwise recurse to
    # daily granularity and need hundreds of requests. At the floor we keep
    # the most recent 250 of the week (sort=datedesc) — still dense.
    # Also stop splitting once the per-company request budget is spent:
    # partial coverage of a mega-cap is fine, an endless fetch is not.
    if (len(arts) >= MAXRECORDS and (end - start).days > 7
            and counter["n"] < MAX_REQUESTS):
        mid = start + (end - start) / 2
        return (gdelt(query, start, mid, counter)
                + gdelt(query, mid + timedelta(1), end, counter))
    return arts


def month_chunks(start: date, end: date, step_days: int = 60):
    """~2-month chunks. NOTE: do NOT widen this — under load GDELT silently
    truncates artlist responses, and wide windows lose data (observed:
    180d one-shot returned 3-5x fewer rows than 60d chunks for the same
    company). Sparse companies dominate; dense ones split on the 250 cap."""
    cur = start
    while cur <= end:
        yield cur, min(end, cur + timedelta(days=step_days - 1))
        cur = cur + timedelta(days=step_days)


def fetch_company(row: dict) -> dict:
    ref = datetime.strptime(row["ref_date"], "%Y-%m-%d").date()
    w_start, w_end = ref - timedelta(days=WINDOW_DAYS), ref - timedelta(days=1)
    tokens = name_tokens(row["query_name"])
    counter = {"n": 0}
    seen, kept = set(), []
    for m_start, m_end in month_chunks(w_start, w_end):
        if counter["n"] >= MAX_REQUESTS:
            break  # budget spent: keep partial coverage
        for art in gdelt(row["query_name"], m_start, m_end, counter):
            title = (art.get("title") or "").strip()
            if not title or not title_mentions(title, tokens):
                continue
            d = art.get("seendate", "")[:8]
            if not d:
                continue
            d = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
            if not (w_start.isoformat() <= d <= w_end.isoformat()):
                continue
            key = (d, norm(title))
            if key in seen:
                continue
            seen.add(key)
            kept.append({"date": d, "title": title,
                         "url": art.get("url", ""),
                         "publisher": art.get("domain", "")})
    kept.sort(key=lambda r: (r["date"], r["title"]))
    return {"window": {"start": w_start.isoformat(), "end": w_end.isoformat()},
            "news": kept, "n_requests": counter["n"]}


def process_company(i: int, total: int, row: dict) -> tuple:
    """Fetch one company; returns (i, row, n_rows, status)."""
    key = re.sub(r"[^A-Za-z0-9_-]", "", row["symbol"] or row["cik"])
    out_path = os.path.join(OUT_DIR, f"{row['type'][:3]}_{key}.jsonl")
    try:
        res = fetch_company(row)
    except Exception as e:
        print(f"[{i+1}/{total}] {row['name'][:40]:40s} ERROR {e}",
              flush=True)
        return i, row, 0, f"ERROR {e}"
    n = len(res["news"])
    if n >= MIN_NEWS:
        with open(out_path, "w", encoding="utf-8") as f:
            for r in res["news"]:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        with open(out_path + ".meta.json", "w", encoding="utf-8") as f:
            json.dump({"window": res["window"],
                       "n_requests": res["n_requests"]}, f)
        status = "OK"
    else:
        # marker so restarts skip this (deliberately-dropped) company
        open(out_path + ".dropped", "w").close()
        status = f"DROPPED (<{MIN_NEWS})"
    print(f"[{i+1}/{total}] {row['name'][:40]:40s} {n:4d} rows {status} "
          f"({res['n_requests']} reqs)", flush=True)
    return i, row, n, status


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(CANDIDATES, encoding="utf-8") as f:
        companies = list(csv.DictReader(f))
    total = len(companies)
    print(f"{total} candidates")

    results = {}
    todo = []
    for i, row in enumerate(companies):
        key = re.sub(r"[^A-Za-z0-9_-]", "", row["symbol"] or row["cik"])
        out_path = os.path.join(OUT_DIR, f"{row['type'][:3]}_{key}.jsonl")
        if os.path.exists(out_path):
            n = sum(1 for _ in open(out_path))
            results[i] = (row, n, "cached")
        elif os.path.exists(out_path + ".dropped"):
            results[i] = (row, 0, f"DROPPED (<{MIN_NEWS})")
        else:
            todo.append((i, row))
    print(f"{len(results)} cached, {len(todo)} to fetch "
          f"({WORKERS} workers)")

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(process_company, i, total, row)
                for i, row in todo]
        for fut in as_completed(futs):
            i, row, n, status = fut.result()
            results[i] = (row, n, status)

    ok = [s for s in results.values() if s[2] in ("OK", "cached")]
    print(f"\n{len(ok)}/{total} companies pass the density gate")
    with open(os.path.join(BASE, "raw", "news_summary.csv"), "w",
              newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["type", "cik", "name", "symbol", "ref_date", "n_news",
                    "status"])
        for i in sorted(results):
            row, n, status = results[i]
            w.writerow([row["type"], row["cik"], row["name"], row["symbol"],
                        row["ref_date"], n, status])


if __name__ == "__main__":
    main()
