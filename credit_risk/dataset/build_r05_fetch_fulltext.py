#!/usr/bin/env python
"""Step R05: fetch full article text for the contemporary dataset's news.

R02 (GDELT) only discovers headlines + URLs. This step crawls each URL
directly on the publisher's site and extracts the article body with
trafilatura. 2025-2026 links are mostly still live, unlike the v0.x
backfill (2016-2023 links were dead/paywalled).

Input:  contemporary/raw/news/<type>_<key>.jsonl   (date, title, url, publisher)
Output: contemporary/raw/fulltext/<type>_<key>.jsonl
        same rows + text / fetch_status (ok|thin|error) / n_chars

Checkpointing: an existing output file is skipped entirely; re-run the
same command to pick up newly fetched headline files or to retry (delete
the output file first to redo a company).

Run from the repository root:
    .venv/bin/python credit_risk/dataset/build_r05_fetch_fulltext.py
"""

import glob
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import trafilatura

BASE = "credit_risk/dataset/contemporary"
NEWS_DIR = os.path.join(BASE, "raw", "news")
OUT_DIR = os.path.join(BASE, "raw", "fulltext")

WORKERS = 8           # publisher sites are normal web servers, not GDELT
TIMEOUT = 20
MAX_CHARS = 15000
THIN_CHARS = 200      # below this the "article" is a paywall stub / junk

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0.0.0 Safari/537.36")

_tls = threading.local()


def _session():
    if not hasattr(_tls, "s"):
        _tls.s = requests.Session()
        _tls.s.headers.update({"User-Agent": UA})
    return _tls.s


def fetch_text(url: str) -> tuple:
    """-> (text|None, status)."""
    try:
        r = _session().get(url, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code in (401, 402, 403, 404, 410):
            return None, f"http{r.status_code}"
        r.raise_for_status()
    except Exception as e:
        return None, f"error:{type(e).__name__}"
    text = trafilatura.extract(r.text, include_comments=False,
                               include_tables=False)
    if not text:
        return None, "empty"
    text = text[:MAX_CHARS]
    return text, ("ok" if len(text) >= THIN_CHARS else "thin")


def process_file(path: str) -> str:
    key = os.path.basename(path)
    out_path = os.path.join(OUT_DIR, key)
    rows = [json.loads(l) for l in open(path, encoding="utf-8")]
    results = [None] * len(rows)

    def one(idx, row):
        text, status = fetch_text(row["url"])
        row = dict(row)
        row["text"] = text
        row["fetch_status"] = status
        row["n_chars"] = len(text) if text else 0
        results[idx] = row

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(one, i, r) for i, r in enumerate(rows)]
        for f in as_completed(futs):
            f.result()

    with open(out_path, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    ok = sum(1 for r in results if r["fetch_status"] == "ok")
    return f"{key}: {ok}/{len(results)} full text OK"


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    files = sorted(glob.glob(os.path.join(NEWS_DIR, "*.jsonl")))
    files = [f for f in files if not f.endswith(".meta.json")]
    todo = [f for f in files
            if not os.path.exists(os.path.join(OUT_DIR,
                                               os.path.basename(f)))]
    print(f"{len(files)} headline files, {len(todo)} to fetch fulltext")
    for i, path in enumerate(todo):
        t0 = time.time()
        try:
            msg = process_file(path)
        except Exception as e:
            msg = f"{os.path.basename(path)}: ERROR {e}"
        print(f"[{i+1}/{len(todo)}] {msg} ({time.time()-t0:.0f}s)",
              flush=True)


if __name__ == "__main__":
    main()
