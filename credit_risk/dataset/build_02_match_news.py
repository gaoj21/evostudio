"""
Step 02: link bankruptcy events to FNSPID news by company name, and count
news density in the 180 days before each event.

FNSPID news rows carry a Stock_symbol but no company name, and SEC does not
retain tickers of delisted companies — so we link news to companies by
matching normalized company names against news titles and URLs. The dominant
Stock_symbol among a company's matched rows becomes its ticker candidate
(manually reviewable in the output).

Inputs:
    raw/bankruptcy_events.csv   (from build_01_events.py)
    raw/All_external.csv        (FNSPID, streamed in chunks)

Outputs:
    raw/matched_news.csv        (cik, date, symbol, title, url, publisher)
    raw/symbol_year_news_counts.csv  (symbol, year, news_count) — ALL symbols,
                                 for picking negative (alive) companies later
                                 without rescanning the 5.7GB file
    candidates.csv              (cik, company_name, event_date, dominant_symbol,
                                 news_count, news_days) filtered to >= MIN_NEWS
"""

import csv
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import pandas as pd

EVENTS_CSV = "credit_risk/dataset/raw/bankruptcy_events.csv"
NEWS_CSV = "credit_risk/dataset/raw/All_external.csv"
MATCHED_OUT = "credit_risk/dataset/raw/matched_news.csv"
CANDIDATES_OUT = "credit_risk/dataset/candidates.csv"

WINDOW_DAYS = 180
MIN_NEWS = 30
CHUNK_SIZE = 200_000

_SUFFIXES = {
    "inc", "corp", "corporation", "llc", "llp", "lp", "ltd", "co", "company",
    "companies", "group", "holdings", "holding", "plc", "sa", "nv", "usa",
    "us", "u s", "the", "de", "new", "old", "international", "intl", "incorporated",
    "enterprises", "partners", "trust", "reit", "etf", "fund", "class", "a", "b", "c",
}


def normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def company_key(name: str) -> str:
    """Distinctive part of a company name: normalized, legal suffixes removed."""
    name = re.sub(r"\([^)]*\)", " ", name)  # drop parenthetical tickers like "(BRDSQ)"
    tokens = [t for t in normalize(name).split() if t not in _SUFFIXES]
    return " ".join(tokens)


def parse_news_date(raw: str):
    try:
        return datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def main():
    # --- load events, build name index ---
    events = []
    with open(EVENTS_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = company_key(row["company_name"])
            if len(key) < 4:  # too short to match safely
                continue
            events.append({
                **row,
                "key": key,
                "event_dt": datetime.strptime(row["event_date"], "%Y-%m-%d"),
            })
    first_token_index = defaultdict(list)  # first key token -> event indices
    for i, ev in enumerate(events):
        first_token_index[ev["key"].split()[0]].append(i)
    print(f"{len(events)} matchable bankruptcy events, {len(first_token_index)} first-token buckets")

    window_start = {ev["cik"]: ev["event_dt"] - timedelta(days=WINDOW_DAYS) for ev in events}

    matched_rows = []
    # symbol votes per cik (to pick the dominant ticker candidate)
    symbol_votes = defaultdict(Counter)
    # news volume per (symbol, year) across the whole file — used to select
    # negative (alive) company windows in step 03 without rescanning
    symbol_year_counts = Counter()

    usecols = ["Date", "Article_title", "Stock_symbol", "Url", "Publisher"]
    rows_seen = rows_matched = 0
    for chunk in pd.read_csv(NEWS_CSV, usecols=usecols, chunksize=CHUNK_SIZE, on_bad_lines="skip"):
        for date_raw, title, symbol, url, publisher in chunk.itertuples(index=False):
            rows_seen += 1
            news_dt = parse_news_date(str(date_raw))
            if news_dt is not None:
                symbol_year_counts[(str(symbol), news_dt.year)] += 1
            title_n = normalize(str(title))
            if not title_n:
                continue
            tokens = set(title_n.split())
            # candidate events: key's first token appears as a whole token in the title
            candidate_ids = []
            for t in tokens:
                candidate_ids.extend(first_token_index.get(t, ()))
            if not candidate_ids:
                continue
            token_set = tokens | set(normalize(str(url)).split())
            for idx in set(candidate_ids):
                ev = events[idx]
                # whole-token match: every key token must appear as a complete
                # token in the title or URL (no substring false positives)
                if not all(tok in token_set for tok in ev["key"].split()):
                    continue
                if news_dt is None or not (window_start[ev["cik"]] <= news_dt <= ev["event_dt"]):
                    continue
                rows_matched += 1
                symbol_votes[ev["cik"]][str(symbol)] += 1
                matched_rows.append({
                    "cik": ev["cik"],
                    "date": news_dt.strftime("%Y-%m-%d"),
                    "symbol": symbol,
                    "title": title,
                    "url": url,
                    "publisher": publisher,
                })
        print(f"  ...{rows_seen:,} news rows scanned, {rows_matched:,} matched")

    with open(MATCHED_OUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["cik", "date", "symbol", "title", "url", "publisher"])
        writer.writeheader()
        writer.writerows(matched_rows)
    print(f"\n{len(matched_rows):,} matched news rows -> {MATCHED_OUT}")

    counts_out = "credit_risk/dataset/raw/symbol_year_news_counts.csv"
    with open(counts_out, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["symbol", "year", "news_count"])
        for (symbol, year), count in sorted(symbol_year_counts.items()):
            writer.writerow([symbol, year, count])
    print(f"{len(symbol_year_counts):,} (symbol, year) counts -> {counts_out}")

    # --- candidate companies: density filter ---
    by_cik = defaultdict(lambda: {"news": 0, "days": set()})
    for r in matched_rows:
        by_cik[r["cik"]]["news"] += 1
        by_cik[r["cik"]]["days"].add(r["date"])

    event_by_cik = {ev["cik"]: ev for ev in events}
    candidates = []
    for cik, agg in by_cik.items():
        if agg["news"] < MIN_NEWS:
            continue
        dominant_symbol, _ = symbol_votes[cik].most_common(1)[0]
        ev = event_by_cik[cik]
        candidates.append({
            "cik": cik,
            "company_name": ev["company_name"],
            "event_date": ev["event_date"],
            "dominant_symbol": dominant_symbol,
            "news_count": agg["news"],
            "news_days": len(agg["days"]),
        })
    candidates.sort(key=lambda r: -r["news_count"])

    with open(CANDIDATES_OUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["cik", "company_name", "event_date", "dominant_symbol", "news_count", "news_days"])
        writer.writeheader()
        writer.writerows(candidates)
    print(f"{len(candidates)} candidate companies (>= {MIN_NEWS} news in window) -> {CANDIDATES_OUT}")
    for c in candidates[:20]:
        print(f"  {c['company_name']:40s} {c['event_date']} symbol={c['dominant_symbol']:6s} news={c['news_count']}")


if __name__ == "__main__":
    main()
