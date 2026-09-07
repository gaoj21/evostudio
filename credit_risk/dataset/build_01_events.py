"""
Step 01: fetch corporate bankruptcy events from SEC EDGAR full-text search.

An 8-K filing with Item 1.03 ("Bankruptcy or Receivership") is the official,
timestamped public disclosure that a company has entered bankruptcy. Querying
EDGAR FTS for these filings gives exact event dates and CIKs — the ground
truth labels for the credit-risk news dataset.

Output: raw/bankruptcy_events.csv with columns
    cik, company_name, event_date, form, adsh, sics

Notes:
- One company may file multiple 8-K/8-K/A for the same episode; we keep the
  EARLIEST filing per CIK (first public disclosure).
- Companies with multiple distinct episodes ("Chapter 22") are collapsed to
  their first episode in v0.1.
"""

import csv
import json
import time
import urllib.parse
import urllib.request

OUTPUT = "credit_risk/dataset/raw/bankruptcy_events.csv"
USER_AGENT = "EvoAgentX research admin@example.com"
QUERY = '"Item 1.03"'
FORMS = "8-K"
YEAR_RANGE = range(2001, 2024)
PAGE_SIZE = 100


def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def search_year(year: int) -> list:
    """All Item-1.03 8-K filings for one calendar year (paginated)."""
    hits = []
    offset = 0
    while True:
        params = urllib.parse.urlencode({
            "q": QUERY,
            "forms": FORMS,
            "startdt": f"{year}-01-01",
            "enddt": f"{year}-12-31",
            "from": offset,
        })
        data = fetch(f"https://efts.sec.gov/LATEST/search-index?{params}")
        batch = data.get("hits", {}).get("hits", [])
        if not batch:
            break
        hits.extend(batch)
        total = data["hits"]["total"]["value"]
        offset += len(batch)
        if offset >= total or offset >= 10000:  # ES result window guard
            break
        time.sleep(0.3)  # SEC fair-use rate limiting
    return hits


def main():
    events = {}  # cik -> earliest event record
    for year in YEAR_RANGE:
        hits = search_year(year)
        year_new = 0
        for hit in hits:
            src = hit["_source"]
            file_date = src.get("file_date")
            items = src.get("items") or []
            if not any(str(i).startswith("1.03") for i in items):
                continue
            for cik, name in zip(src.get("ciks", []), src.get("display_names", [])):
                clean_name = name.rsplit("(CIK", 1)[0].strip()
                record = {
                    "cik": cik.lstrip("0") or "0",
                    "company_name": clean_name,
                    "event_date": file_date,
                    "form": src.get("form", ""),
                    "adsh": src.get("adsh", ""),
                    "sics": ",".join(src.get("sics", [])),
                }
                if cik not in events or file_date < events[cik]["event_date"]:
                    if cik not in events:
                        year_new += 1
                    events[cik] = record
        print(f"{year}: {len(hits)} filings, {len(events)} unique companies so far")
        time.sleep(0.5)

    rows = sorted(events.values(), key=lambda r: r["event_date"])
    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["cik", "company_name", "event_date", "form", "adsh", "sics"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nSaved {len(rows)} bankruptcy events to {OUTPUT}")


if __name__ == "__main__":
    main()
