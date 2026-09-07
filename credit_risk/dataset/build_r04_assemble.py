#!/usr/bin/env python
"""Step R04: assemble the contemporary dataset (samples + splits + manifest).

Reads:
- contemporary/candidates.csv                      (company list)
- contemporary/raw/fulltext/<type>_<key>.jsonl     (news + body text, R05;
                                                    falls back to R02's
                                                    raw/news headlines-only)
- contemporary/raw/filings/<type>_<key>.jsonl      (EDGAR 8-K bodies, R03)

Only companies that passed R02's density gate (news file exists) become
samples. Schema is the v0.4 schema plus a `filings` channel:

    news:    [{news_id, date, title, url, publisher,
               text, fetch_status, n_chars}]   (text present when R05 ran)
    filings: [{filing_date, form, adsh, items, text}]

Labels and derived day-level risk bands follow the same rules as v0.1+
(window ends the day before ref_date; positive label = bankruptcy at
ref_date). Split: per-class chronological by window.end at 70/15/15, one
company in one split.

Output: contemporary/{samples,train,dev,test}.jsonl + manifest.json

Run from the repository root:
    .venv/bin/python credit_risk/dataset/build_r04_assemble.py
"""

import csv
import hashlib
import json
import os
import re
from collections import Counter

BASE = "credit_risk/dataset/contemporary"
CANDIDATES = os.path.join(BASE, "candidates.csv")
NEWS_DIR = os.path.join(BASE, "raw", "news")
FULLTEXT_DIR = os.path.join(BASE, "raw", "fulltext")
FILINGS_DIR = os.path.join(BASE, "raw", "filings")


def news_id(date_, title, url):
    return hashlib.sha1(f"{date_}|{title}|{url}".encode()).hexdigest()[:16]


def key_of(row):
    return re.sub(r"[^A-Za-z0-9_-]", "", row["symbol"] or row["cik"])


def main():
    candidates = list(csv.DictReader(open(CANDIDATES, encoding="utf-8")))
    samples = []
    dropped = []
    for row in candidates:
        key = key_of(row)
        news_path = os.path.join(NEWS_DIR, f"{row['type'][:3]}_{key}.jsonl")
        if not os.path.exists(news_path):
            dropped.append((row["name"], "failed density gate / no news"))
            continue
        # prefer the R05 fulltext file (same rows + text/fetch_status)
        ft_path = os.path.join(FULLTEXT_DIR,
                               f"{row['type'][:3]}_{key}.jsonl")
        src_path = ft_path if os.path.exists(ft_path) else news_path
        news = [json.loads(l) for l in open(src_path, encoding="utf-8")]
        n_fulltext = sum(1 for r in news if r.get("fetch_status") == "ok")
        for r in news:
            r["news_id"] = news_id(r["date"], r["title"], r["url"])
        filings_path = os.path.join(FILINGS_DIR,
                                    f"{row['type'][:3]}_{key}.jsonl")
        filings = []
        if os.path.exists(filings_path):
            filings = [json.loads(l)
                       for l in open(filings_path, encoding="utf-8")]
        meta_path = news_path + ".meta.json"
        window = (json.load(open(meta_path))["window"]
                  if os.path.exists(meta_path) else None)

        if row["type"] == "positive":
            sid = f"pos_{row['cik']}_{row['ref_date']}"
            label = {"event": "bankruptcy", "event_date": row["ref_date"]}
        else:
            sid = f"neg_{key}_{row['ref_date'][:4]}"
            label = {"event": None, "event_date": None}

        samples.append({
            "sample_id": sid,
            "type": row["type"],
            "company": {"name": row["name"], "cik": row["cik"],
                        "symbol": row["symbol"] or None,
                        "query_name": row["query_name"]},
            "window": window,
            "label": label,
            "split": None,
            "news": news,
            "filings": filings,
        })

    # per-class chronological 70/15/15 split by window.end
    for cls in ("positive", "negative"):
        group = sorted([s for s in samples if s["type"] == cls],
                       key=lambda s: s["window"]["end"])
        n = len(group)
        n_train = int(round(n * 0.70))
        n_dev = int(round(n * 0.15))
        for i, s in enumerate(group):
            s["split"] = ("train" if i < n_train else
                          "dev" if i < n_train + n_dev else "test")

    os.makedirs(BASE, exist_ok=True)
    with open(os.path.join(BASE, "samples.jsonl"), "w",
              encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    for split in ("train", "dev", "test"):
        with open(os.path.join(BASE, f"{split}.jsonl"), "w",
                  encoding="utf-8") as f:
            for s in samples:
                if s["split"] == split:
                    f.write(json.dumps(s, ensure_ascii=False) + "\n")

    stats = Counter()
    for s in samples:
        stats[f"{s['type'][:3]}_{s['split']}"] += 1
        stats["news_rows"] += len(s["news"])
        stats["news_fulltext_ok"] += sum(
            1 for r in s["news"] if r.get("fetch_status") == "ok")
        stats["filings"] += len(s["filings"])
    manifest = {
        "version": "contemporary-0.1",
        "era": "2025-01 .. 2026-08 (news: GDELT DOC 2.0; labels: EDGAR "
               "8-K Item 1.03, verified against filing bodies)",
        "samples": len(samples),
        "positive": sum(1 for s in samples if s["type"] == "positive"),
        "negative": sum(1 for s in samples if s["type"] == "negative"),
        "news_rows": stats["news_rows"],
        "news_fulltext_ok": stats["news_fulltext_ok"],
        "filings_total": stats["filings"],
        "splits": {k: v for k, v in stats.items() if "_" in k
                   and not k.startswith("news")},
        "dropped_candidates": [{"name": n, "reason": r}
                               for n, r in dropped],
    }
    with open(os.path.join(BASE, "manifest.json"), "w",
              encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"samples: {len(samples)} "
          f"({manifest['positive']} pos / {manifest['negative']} neg)")
    print(f"news rows: {stats['news_rows']}, 8-K filings: {stats['filings']}")
    print("splits:", {k: v for k, v in sorted(stats.items())
                      if '_' in k})
    print(f"dropped: {len(dropped)} (see manifest)")
    empt = [s['sample_id'] for s in samples if not s['filings']]
    print(f"samples with 0 filings: {len(empt)}")


if __name__ == "__main__":
    main()
