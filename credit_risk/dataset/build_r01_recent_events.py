#!/usr/bin/env python
"""Step R01: recent (2025+) bankruptcy events for the contemporary dataset.

Same label source as build_01 + build_02b, but restricted to the recent era
so the news side can be served by GDELT (which covers 2017–present):

1. EDGAR full-text search for 8-K filings with Item 1.03, 2025-01-01 .. today
2. earliest filing per CIK (first public disclosure of the episode)
3. verify each candidate against the 8-K BODY (V1 item heading + V2
   bankruptcy keywords), reusing build_02b_verify_events.fetch_submission

Output: contemporary/events_recent.csv
    cik, company_name, event_date, form, adsh, verdict, subsidiary_mention

Run from the repository root:
    .venv/bin/python credit_risk/dataset/build_r01_recent_events.py
"""

import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_02b_verify_events import (  # noqa: E402
    BANKRUPT_RE, CODEBTOR_RE, ITEM_RE, SUBSIDIARY_RE,
    fetch_submission, html_to_text,
)

OUT_DIR = "credit_risk/dataset/contemporary"
OUTPUT = os.path.join(OUT_DIR, "events_recent.csv")
USER_AGENT = "EvoAgentX research admin@example.com"
START_DT, END_DT = "2025-01-01", "2026-08-26"
PAUSE = 0.3  # EDGAR fair-use


def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def search_events() -> list:
    """All Item-1.03 8-K filings in the date range (paginated)."""
    hits = []
    offset = 0
    while True:
        params = urllib.parse.urlencode({
            "q": '"Item 1.03"',
            "forms": "8-K",
            "startdt": START_DT,
            "enddt": END_DT,
            "from": offset,
        })
        data = fetch(f"https://efts.sec.gov/LATEST/search-index?{params}")
        batch = data.get("hits", {}).get("hits", [])
        if not batch:
            break
        hits.extend(batch)
        total = data["hits"]["total"]["value"]
        offset += len(batch)
        if offset >= total or offset >= 10000:
            break
        time.sleep(PAUSE)
    return hits


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    hits = search_events()
    print(f"{len(hits)} Item-1.03 filings {START_DT}..{END_DT}")

    events = {}  # cik -> earliest record
    for hit in hits:
        src = hit["_source"]
        file_date = src.get("file_date")
        items = src.get("items") or []
        if not any(str(i).startswith("1.03") for i in items):
            continue
        for cik, name in zip(src.get("ciks", []), src.get("display_names", [])):
            record = {
                "cik": cik.lstrip("0") or "0",
                "company_name": name.rsplit("(CIK", 1)[0].strip(),
                "event_date": file_date,
                "form": src.get("form", ""),
                "adsh": src.get("adsh", ""),
            }
            if cik not in events or file_date < events[cik]["event_date"]:
                events[cik] = record
    print(f"{len(events)} unique companies; verifying against 8-K bodies…")

    rows = []
    for i, ev in enumerate(sorted(events.values(),
                                  key=lambda r: r["event_date"])):
        try:
            text = html_to_text(fetch_submission(ev["cik"], ev["adsh"]))
            m = ITEM_RE.search(text)
            v1 = m is not None
            v2 = BANKRUPT_RE.search(text) is not None
            subsidiary = ""
            if m:
                section = text[m.start():m.start() + 3000]
                if SUBSIDIARY_RE.search(section):
                    subsidiary = ("co-debtor" if CODEBTOR_RE.search(section)
                                  else "review")
            verdict = "REAL" if (v1 and v2) else "FALSE_MATCH"
        except Exception as e:
            verdict, subsidiary = f"ERROR: {e}", ""
        rows.append({**ev, "verdict": verdict,
                     "subsidiary_mention": subsidiary})
        print(f"  [{i+1}/{len(events)}] {ev['event_date']} "
              f"{ev['company_name'][:42]:42s} {verdict}")
        time.sleep(PAUSE)

    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["cik", "company_name",
                                               "event_date", "form", "adsh",
                                               "verdict",
                                               "subsidiary_mention"])
        writer.writeheader()
        writer.writerows(rows)
    n_real = sum(1 for r in rows if r["verdict"] == "REAL")
    print(f"\n{n_real}/{len(rows)} REAL -> {OUTPUT}")


if __name__ == "__main__":
    main()
