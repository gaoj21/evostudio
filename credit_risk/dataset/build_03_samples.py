"""
Step 03: build dataset v0.1 samples (company-windows) from matched news.

- Positives: candidate bankrupt companies (from step 02), after a
  name-match VERIFICATION GATE (name matching on titles is noisy — generic
  keys like "bank" / "at t" produce garbage). A candidate is accepted when:
      R1  one Stock_symbol dominates its matched rows (top_share >= 0.30), or
      R2  the multi-token company key appears as a contiguous PHRASE in
          >= 50% of matched titles, or
      R3  a single-token key (len >= 7, not a generic English word) appears
          as a phrase in >= 60% of matched titles.
  Accepted rows are then cleaned at row level: keep rows whose symbol is the
  confirmed dominant symbol, or whose title contains the key phrase.
  Duplicate events (same symbol/key + same event_date, e.g. two CIKs of one
  company) are collapsed.
  The full accept/reject table with reasons lands in candidates_review.csv.
- Window = [event_date - 180d, event_date - 1d]. News ON the event date is
  excluded: same-day coverage may already report the filing (lookahead).
- Negatives: alive (symbol, year) pairs picked from symbol_year_news_counts
  (same era, comparable density, symbols not linked to any bankrupt
  company). Their news is extracted in ONE extra pass over All_external.csv.
- Per-day risk labels are NOT baked in: they are derived downstream from
  days_to_event (see README risk bands), so the same sample supports both
  verdict-level and trajectory-level evaluation.

Output: v0.1/samples.jsonl + manifest.json, candidates_review.csv
"""

import csv
import hashlib
import json
import random
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import pandas as pd

CANDIDATES_CSV = "credit_risk/dataset/candidates.csv"
EVENT_VERIFICATION_CSV = "credit_risk/dataset/raw/event_verification.csv"
EVENT_OVERRIDES_CSV = "credit_risk/dataset/event_overrides.csv"
MATCHED_NEWS_CSV = "credit_risk/dataset/raw/matched_news.csv"
SYMBOL_COUNTS_CSV = "credit_risk/dataset/raw/symbol_year_news_counts.csv"
NEWS_CSV = "credit_risk/dataset/raw/All_external.csv"
REVIEW_CSV = "credit_risk/dataset/candidates_review.csv"
OUT_DIR = "credit_risk/dataset/v0.1"

WINDOW_DAYS = 180
NEGATIVE_RATIO = 2          # negatives per positive
MIN_NEGATIVE_NEWS = 30      # same density bar as positives
MIN_POSITIVE_NEWS = 30      # after row-level cleanup
RANDOM_SEED = 42
CHUNK_SIZE = 200_000

# ETFs / leveraged funds are not companies — their news streams are
# macro/sector commentary, useless as "alive company" negatives
NEGATIVE_SYMBOL_BLOCKLIST = {
    "DBE", "EEME", "EWL", "EWZ", "FAZ", "KWEB", "VFH", "XLK",
    "SPY", "QQQ", "DIA", "IWM", "EEM", "EFA", "VWO", "TLT", "GLD", "SLV",
    "USO", "UNG", "XLE", "XLF", "XLV", "XLI", "XLP", "XLU", "XLB", "XLRE",
    "XLC", "XBI", "IBB", "SMH", "ARKK", "UVXY", "SQQQ", "TQQQ", "UPRO",
    "SH", "PSQ", "DOG", "SDS", "TZA", "FAS", "JNUG", "NUGT", "DUST",
}

TOP_SYM_SHARE_MIN = 0.30    # R1: symbol consensus threshold
PHRASE_RATIO_MIN = 0.50     # R2: phrase-in-title ratio for multi-token keys
PHRASE_RATIO_SINGLE = 0.60  # R3: stricter ratio for single-token keys

# single-token keys that are common English words — never trust without
# symbol confirmation (compiled from eyeballing build_02 output)
GENERIC_SINGLE = {
    "bank", "china", "energy", "life", "local", "fuel", "liquid", "unit",
    "stone", "champion", "concrete", "community", "hydrogen", "blockbuster",
    "noble", "ambassadors", "security", "franklin", "patriot", "allied",
    "united", "global", "national", "first", "american", "general",
}

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
    name = re.sub(r"\([^)]*\)", " ", name)  # drop parenthetical tickers
    tokens = [t for t in normalize(name).split() if t not in _SUFFIXES]
    return " ".join(tokens)


def news_id(date: str, title: str, url: str) -> str:
    return hashlib.sha1(f"{date}|{title}|{url}".encode("utf-8")).hexdigest()[:16]


def contains_phrase(title_norm: str, key: str) -> bool:
    return f" {key} " in f" {title_norm} "


def verify_candidates():
    """Apply the acceptance gate; return (accepted, review_rows)."""
    # drop events whose 8-K failed full-text verification (step 02b):
    # filings merely citing "Item 1.03", or a parent disclosing a
    # subsidiary's bankruptcy rather than its own
    verified = {}
    try:
        with open(EVENT_VERIFICATION_CSV, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                verified[row["cik"]] = row
    except FileNotFoundError:
        print(f"WARNING: {EVENT_VERIFICATION_CSV} not found, "
              f"events are NOT full-text verified")
    # manual overrides win over automated verdicts (see file for reasons)
    try:
        with open(EVENT_OVERRIDES_CSV, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["cik"] in verified:
                    verified[row["cik"]]["verdict"] = (
                        "REAL" if row["verdict"] == "REAL" else "FALSE_MATCH")
                    verified[row["cik"]]["subsidiary_mention"] = ""
    except FileNotFoundError:
        pass

    candidates = {}
    with open(CANDIDATES_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ver = verified.get(row["cik"])
            # verdict gate only: the subsidiary_mention=="review" bucket was
            # manually audited (all real registrant bankruptcies; the single
            # subsidiary-only case, LendingTree, is overridden to REJECT in
            # event_overrides.csv)
            if ver is not None and ver["verdict"] != "REAL":
                continue
            candidates[row["cik"]] = row

    news_by_cik = defaultdict(list)
    with open(MATCHED_NEWS_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            news_by_cik[row["cik"]].append(row)

    accepted, review = [], []
    for cik, cand in candidates.items():
        rows = news_by_cik[cik]
        key = company_key(cand["company_name"])
        key_tokens = key.split()

        syms = [str(r["symbol"]) for r in rows
                if str(r["symbol"]).lower() not in ("nan", "none", "")]
        top_sym, top_share = (Counter(syms).most_common(1)[0][0],
                              Counter(syms).most_common(1)[0][1] / len(rows)) if syms else (None, 0.0)

        phrase_hits = sum(1 for r in rows if contains_phrase(normalize(str(r["title"])), key))
        phrase_ratio = phrase_hits / len(rows)

        reason = None
        if top_share >= TOP_SYM_SHARE_MIN:
            reason = f"R1 symbol {top_sym} share={top_share:.2f}"
        elif len(key_tokens) >= 2 and phrase_ratio >= PHRASE_RATIO_MIN:
            reason = f"R2 phrase '{key}' ratio={phrase_ratio:.2f}"
        elif (len(key_tokens) == 1 and len(key) >= 7
              and key not in GENERIC_SINGLE and phrase_ratio >= PHRASE_RATIO_SINGLE):
            reason = f"R3 single-token '{key}' ratio={phrase_ratio:.2f}"

        keep = reason is not None
        review.append({
            "cik": cik, "company_name": cand["company_name"],
            "event_date": cand["event_date"], "key": key,
            "raw_news": len(rows), "top_sym": top_sym,
            "top_share": f"{top_share:.3f}", "phrase_ratio": f"{phrase_ratio:.3f}",
            "verdict": "ACCEPT" if keep else "REJECT",
            "reason": reason or "no symbol consensus, no phrase dominance",
        })
        if not keep:
            continue

        # row-level cleanup: confirmed-symbol rows OR phrase-in-title rows
        clean = [
            r for r in rows
            if (top_share >= TOP_SYM_SHARE_MIN and str(r["symbol"]) == top_sym)
            or contains_phrase(normalize(str(r["title"])), key)
        ]
        accepted.append({**cand, "key": key, "symbol": top_sym, "rows": clean, "reason": reason})

    # collapse duplicate events: same (symbol or key) + same event_date
    seen, deduped = set(), []
    for a in sorted(accepted, key=lambda a: -len(a["rows"])):
        sig = (a["symbol"] or a["key"], a["event_date"])
        if sig in seen:
            continue
        seen.add(sig)
        deduped.append(a)

    review.sort(key=lambda r: (r["verdict"], -int(r["raw_news"])))
    with open(REVIEW_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(review[0].keys()))
        writer.writeheader()
        writer.writerows(review)
    return deduped


def load_positives():
    accepted = verify_candidates()
    print(f"{len(accepted)} candidates passed the verification gate "
          f"(review table -> {REVIEW_CSV})")

    samples = []
    for cand in accepted:
        event_date = datetime.strptime(cand["event_date"], "%Y-%m-%d")
        window_start = event_date - timedelta(days=WINDOW_DAYS)
        window_end = event_date - timedelta(days=1)  # exclude event-day coverage
        news = [
            {
                "news_id": news_id(r["date"], r["title"], r["url"]),
                "date": r["date"],
                "title": r["title"],
                "url": r["url"],
                "publisher": r["publisher"],
            }
            for r in sorted(cand["rows"], key=lambda r: r["date"])
            if window_start.strftime("%Y-%m-%d") <= r["date"] <= window_end.strftime("%Y-%m-%d")
        ]
        if len(news) < MIN_POSITIVE_NEWS:
            print(f"  drop {cand['company_name']}: only {len(news)} clean news in window")
            continue
        samples.append({
            "sample_id": f"pos_{cand['cik']}_{cand['event_date']}",
            "type": "positive",
            "company": {
                "name": cand["company_name"],
                "cik": cand["cik"],
                "symbol": cand["symbol"],
            },
            "window": {
                "start": window_start.strftime("%Y-%m-%d"),
                "end": window_end.strftime("%Y-%m-%d"),
            },
            "label": {"event": "bankruptcy", "event_date": cand["event_date"]},
            "news": news,
        })
    return samples


def pick_negatives(positive_samples):
    """Alive (symbol, year) pairs with comparable density, same era."""
    bankrupt_symbols = {s["company"]["symbol"] for s in positive_samples}
    event_years = [int(s["label"]["event_date"][:4]) for s in positive_samples]
    year_min, year_max = min(event_years), max(event_years)

    pairs = []
    with open(SYMBOL_COUNTS_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            symbol, year, count = row["symbol"], int(row["year"]), int(row["news_count"])
            if symbol in bankrupt_symbols or symbol in NEGATIVE_SYMBOL_BLOCKLIST:
                continue
            if not (year_min <= year <= year_max):
                continue
            if count >= MIN_NEGATIVE_NEWS:
                pairs.append((symbol, year, count))
    random.seed(RANDOM_SEED)
    random.shuffle(pairs)
    # keep per-symbol uniqueness, then take the requested volume
    seen, chosen = set(), []
    for symbol, year, count in pairs:
        if symbol in seen:
            continue
        seen.add(symbol)
        chosen.append((symbol, year))
        if len(chosen) >= NEGATIVE_RATIO * len(positive_samples):
            break
    return chosen


def extract_negative_news(chosen):
    """One pass over All_external.csv to collect news for negative windows."""
    windows = {}
    for symbol, year in chosen:
        end = datetime(year, 12, 20)
        windows[symbol] = (end - timedelta(days=WINDOW_DAYS), end, year)
    news_by_symbol = defaultdict(list)
    usecols = ["Date", "Article_title", "Stock_symbol", "Url", "Publisher"]
    for chunk in pd.read_csv(NEWS_CSV, usecols=usecols, chunksize=CHUNK_SIZE, on_bad_lines="skip"):
        for date_raw, title, symbol, url, publisher in chunk.itertuples(index=False):
            symbol = str(symbol)
            if symbol not in windows:
                continue
            try:
                news_dt = datetime.strptime(str(date_raw)[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            start, end, _ = windows[symbol]
            if start <= news_dt <= end:
                news_by_symbol[symbol].append({
                    "news_id": news_id(news_dt.strftime("%Y-%m-%d"), str(title), str(url)),
                    "date": news_dt.strftime("%Y-%m-%d"),
                    "title": str(title),
                    "url": str(url),
                    "publisher": str(publisher),
                })
        print(f"  ...scanning for negative windows, {sum(len(v) for v in news_by_symbol.values()):,} rows collected")

    samples = []
    for symbol, year in chosen:
        start, end, _ = windows[symbol]
        news = sorted(news_by_symbol.get(symbol, []), key=lambda r: r["date"])
        if len(news) < MIN_NEGATIVE_NEWS:
            continue
        samples.append({
            "sample_id": f"neg_{symbol}_{year}",
            "type": "negative",
            "company": {"name": None, "cik": None, "symbol": symbol},
            "window": {"start": start.strftime("%Y-%m-%d"), "end": end.strftime("%Y-%m-%d")},
            "label": {"event": None, "event_date": None},
            "news": news,
        })
    return samples


def main():
    import os
    os.makedirs(OUT_DIR, exist_ok=True)

    positives = load_positives()
    print(f"{len(positives)} positive samples")
    chosen = pick_negatives(positives)
    print(f"{len(chosen)} negative (symbol, year) windows selected")
    negatives = extract_negative_news(chosen)
    print(f"{len(negatives)} negative samples kept (>= {MIN_NEGATIVE_NEWS} news)")

    samples = positives + negatives
    with open(f"{OUT_DIR}/samples.jsonl", "w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    manifest = {
        "version": "0.1",
        "window_days": WINDOW_DAYS,
        "n_positive": len(positives),
        "n_negative": len(negatives),
        "total_news": sum(len(s["news"]) for s in samples),
        "event_date_range": [
            min(s["label"]["event_date"] for s in positives),
            max(s["label"]["event_date"] for s in positives),
        ] if positives else None,
        "label_source": "SEC EDGAR 8-K Item 1.03 (Bankruptcy or Receivership)",
        "news_source": "FNSPID All_external.csv (Benzinga etc.)",
        "verification_gate": "R1 symbol share>=0.30 | R2 phrase ratio>=0.50 (multi-token) | R3 phrase ratio>=0.60 (single-token, non-generic)",
    }
    with open(f"{OUT_DIR}/manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
