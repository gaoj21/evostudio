#!/usr/bin/env python
"""Step R03: fetch in-window 8-K filings (full text) per company from EDGAR.

The contemporary dataset's second channel: the company's own disclosures.
For each candidate company we pull every 8-K / 8-K/A filed inside the
sample window from EDGAR, parse the SGML full submission, keep the 8-K body
(same approach as build_02b), extract the Item headings, and store the full
text (capped).

EDGAR keeps filings of delisted companies, so bankrupt positives work the
same as alive negatives. Free, no key, fair-access pauses.

Input:  contemporary/candidates.csv (same file as R02)
Output: contemporary/raw/filings/<type>_<key>.jsonl, one filing per line:
    {filing_date, form, adsh, items, text}

Run from the repository root:
    .venv/bin/python credit_risk/dataset/build_r03_fetch_8k.py
"""

import csv
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_02b_verify_events import fetch_submission, html_to_text  # noqa: E402

BASE = "credit_risk/dataset/contemporary"
CANDIDATES = os.path.join(BASE, "candidates.csv")
OUT_DIR = os.path.join(BASE, "raw", "filings")

UA = {"User-Agent": "EvoAgentX research (contact: research@example.com)"}
PAUSE = 0.3
WINDOW_DAYS = 180
FORMS = {"8-K", "8-K/A"}
TEXT_CAP = 30000  # chars; 8-K bodies are usually 2-10 KB

ITEM_HEADING_RE = re.compile(r"item\s+(\d\.\d\d)", re.I)


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def filings_in_window(cik: str, w_start: str, w_end: str) -> list:
    """All 8-K/8-K/A filings of CIK with filingDate in [w_start, w_end]."""
    data = _get_json(
        f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json")
    recent = data.get("filings", {}).get("recent", {})
    out = []
    for form, fdate, adsh in zip(recent.get("form", []),
                                 recent.get("filingDate", []),
                                 recent.get("accessionNumber", [])):
        if form in FORMS and w_start <= fdate <= w_end:
            out.append({"filing_date": fdate, "form": form, "adsh": adsh})
    return sorted(out, key=lambda r: r["filing_date"])


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    companies = list(csv.DictReader(open(CANDIDATES, encoding="utf-8")))

    for i, row in enumerate(companies):
        key = re.sub(r"[^A-Za-z0-9_-]", "", row["symbol"] or row["cik"])
        out_path = os.path.join(OUT_DIR, f"{row['type'][:3]}_{key}.jsonl")
        if os.path.exists(out_path):
            print(f"[{i+1}/{len(companies)}] {row['name'][:40]:40s} cached")
            continue
        ref = datetime.strptime(row["ref_date"], "%Y-%m-%d").date()
        w_end = ref - timedelta(days=1)
        w_start = ref - timedelta(days=WINDOW_DAYS)
        try:
            filings = filings_in_window(row["cik"], w_start.isoformat(),
                                        w_end.isoformat())
        except Exception as e:
            print(f"[{i+1}/{len(companies)}] {row['name'][:40]:40s} "
                  f"submissions ERROR {e}")
            continue
        time.sleep(PAUSE)

        lines = []
        for f in filings:
            try:
                text = html_to_text(fetch_submission(row["cik"], f["adsh"]))
            except Exception as e:
                print(f"    {f['filing_date']} {f['form']} fetch ERROR {e}")
                time.sleep(PAUSE)
                continue
            items = sorted(set(m.lower()
                               for m in ITEM_HEADING_RE.findall(text)))
            lines.append({"filing_date": f["filing_date"],
                          "form": f["form"], "adsh": f["adsh"],
                          "items": items, "text": text[:TEXT_CAP]})
            time.sleep(PAUSE)

        with open(out_path, "w", encoding="utf-8") as fo:
            for ln in lines:
                fo.write(json.dumps(ln, ensure_ascii=False) + "\n")
        print(f"[{i+1}/{len(companies)}] {row['name'][:40]:40s} "
              f"{len(lines)} 8-K filings")


if __name__ == "__main__":
    main()
