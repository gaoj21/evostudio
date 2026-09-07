#!/usr/bin/env python
"""Step R02a: build contemporary/candidates.csv (the company list).

Positives: all REAL Item 1.03 events from build_r01 (2025-01 .. 2026-08),
minus same-episode subsidiary duplicates (QVC INC under QVC Group; Hughes
under EchoStar). query_name is auto-derived from the EDGAR legal name with
manual overrides where the newsworthy name differs (WW International ->
WeightWatchers, QVC Group -> QVC, ...).

Negatives: a fixed hand-picked list of ~24 alive, well-covered companies —
deliberately mixing healthy names with stressed-but-alive ones (JetBlue,
AMC, Kohl's, Plug Power, ...), which are the hard negatives for credit-risk
eval. CIKs resolved via SEC company_tickers.json. Each negative's ref_date
(window end) is sampled from the positive event dates so both classes cover
the same calendar period (controls for the macro environment).

Output: contemporary/candidates.csv
    type, cik, name, query_name, symbol, ref_date

Run from the repository root:
    .venv/bin/python credit_risk/dataset/build_r02_candidates.py
"""

import csv
import json
import os
import random
import re

BASE = "credit_risk/dataset/contemporary"
EVENTS = os.path.join(BASE, "events_recent.csv")
TICKERS = os.path.join(BASE, "raw", "company_tickers.json")
OUTPUT = os.path.join(BASE, "candidates.csv")

# same-episode subsidiary duplicates to drop (keep the parent CIK)
DROP_CIKS = {"1254699",  # QVC INC (episode of QVC Group 1355096)
             "1533758"}  # Hughes Satellite Systems (episode of EchoStar 1415404)

NAME_STOP = {"inc", "corp", "corporation", "group", "co", "company", "ltd",
             "llc", "lp", "holdings", "holding", "the", "plc", "sa", "nv",
             "trust", "partners", "acquisition", "incorporated", "intl",
             "international"}

# legal name -> the name news actually uses
QUERY_OVERRIDES = {
    "WW INTERNATIONAL": "WeightWatchers",
    "QVC Group": "QVC",
    "23andMe Holding Co": "23andMe",
    "WOLFSPEED": "Wolfspeed",
    "Container Store Group": "Container Store",
    "Sonder Holdings": "Sonder",
    "Luminar Technologies": "Luminar",
    "Sunnova Energy International": "Sunnova",
    "Li-Cycle Holdings Corp": "Li-Cycle",
    "Mondee Holdings": "Mondee",
    "BurgerFi International": "BurgerFi",
    "Danimer Scientific": "Danimer",
    "Amergent Hospitality Group": "Amergent",
    "iRobot": "iRobot",
    "Nikola Corp": "Nikola",
    "Spirit Airlines": "Spirit Airlines",
}

# negatives: (symbol, query_name) — alive companies, mixed health
NEGATIVES = [
    ("UAL", "United Airlines"), ("JBLU", "JetBlue"), ("ULCC", "Frontier Airlines"),
    ("TSLA", "Tesla"), ("RIVN", "Rivian"), ("LCID", "Lucid Motors"),
    ("M", "Macy's"), ("KSS", "Kohl's"), ("W", "Wayfair"),
    ("PFE", "Pfizer"), ("MRNA", "Moderna"), ("BNTX", "BioNTech"),
    ("RUN", "Sunrun"), ("PLUG", "Plug Power"),
    ("SBGI", "Sinclair"), ("IHRT", "iHeartMedia"),
    ("WEN", "Wendy's"), ("DIN", "Dine Brands"),
    ("MARA", "MARA Holdings"), ("HLT", "Hilton"), ("ABNB", "Airbnb"),
    ("AMC", "AMC"), ("GME", "GameStop"), ("BYND", "Beyond Meat"),
]


def derive_query_name(legal_name: str) -> str:
    base = re.sub(r"\([^)]*\)", "", legal_name).strip().rstrip(".")
    base = re.sub(r"/DE\b|,|\.", "", base).strip()
    for full, short in QUERY_OVERRIDES.items():
        if base.lower().startswith(full.lower()):
            return short
    toks = [t for t in base.split()
            if t.lower() not in NAME_STOP]
    return " ".join(toks[:2]) if toks else base


def main():
    events = [r for r in csv.DictReader(open(EVENTS, encoding="utf-8"))
              if r["verdict"] == "REAL" and r["cik"] not in DROP_CIKS]
    print(f"{len(events)} positive candidates")

    rows = []
    for ev in events:
        sym_m = re.search(r"\(([A-Z, ]+)\)", ev["company_name"])
        symbol = (sym_m.group(1).split(",")[0].strip() if sym_m else "")
        rows.append({
            "type": "positive",
            "cik": ev["cik"],
            "name": ev["company_name"],
            "query_name": derive_query_name(ev["company_name"]),
            "symbol": symbol,
            "ref_date": ev["event_date"],
        })

    tickers = {v["ticker"]: v for v in json.load(open(TICKERS)).values()}
    event_dates = [r["ref_date"] for r in rows]
    random.seed(7)
    for sym, qname in NEGATIVES:
        t = tickers.get(sym)
        if not t:
            print(f"  !! {sym} not in company_tickers.json")
            continue
        rows.append({
            "type": "negative",
            "cik": str(t["cik_str"]),
            "name": t["title"],
            "query_name": qname,
            "symbol": sym,
            "ref_date": random.choice(event_dates),
        })

    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["type", "cik", "name",
                                          "query_name", "symbol", "ref_date"])
        w.writeheader()
        w.writerows(rows)
    n_pos = sum(1 for r in rows if r["type"] == "positive")
    print(f"{n_pos} positives + {len(rows) - n_pos} negatives -> {OUTPUT}")
    print("\nquery_name spot check:")
    for r in rows[:10] + rows[-6:]:
        print(f"  {r['type'][:3]}  {r['name'][:40]:40s} -> {r['query_name']}")


if __name__ == "__main__":
    main()
