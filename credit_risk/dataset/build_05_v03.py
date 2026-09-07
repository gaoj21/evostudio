#!/usr/bin/env python
"""build_05_v03.py — v0.2 -> v0.3 (annotation only, no sample/row changes).

Evaluating the pipeline on v0.2 surfaced three match-noise failure modes in
the news streams (see README "Known limitations"):

1. roundup wires   — "X and Y among Energy/Materials gainers; ..." daily
   gainers/losers roundups. The company IS mentioned, but the row carries no
   company-specific information. (pos_73887 Bristow: 61/74 rows)
2. person mentions — the company key token is also a surname: "Washington
   REIT's (WRE) CEO Paul McDermott ..." matched McDermott International.
   Wrong entity entirely. (pos_708819)
3. generic-name / foreign-subject — the company key phrase is a generic
   industry phrase ("Global Healthcare") so rows about OTHER companies
   ("Codexis (CDXS) Presents At UBS Global Healthcare Conference") match.
   (pos_727346: essentially zero real news)

v0.3 ANNOTATES, it does not delete: every news row gets `noise_kind`
(null | "foreign_subject" | "person_mention" | "roundup" | "generic_ctx")
and every sample gets a `news_quality` block:

    "news_quality": {
      "rows": 74, "centric_rows": 13, "noise_share": 0.824,
      "flags": ["noisy", "thin_centric"]
    }

Sample flags:
  - "generic_name"   all key tokens of the company name are generic
                     industry words -> name matching is unreliable
  - "noisy"          noise_share > 0.5
  - "thin_centric"   centric_rows < 15 over the ~180d window

Samples, windows, splits and row order are byte-identical to v0.2 (asserted
at build time), so v0.2 baselines remain comparable; evaluators can either
exclude flagged samples or drop noise rows via `noise_kind`.

Run from the repository root:
    .venv/bin/python credit_risk/dataset/build_05_v03.py
"""

import json
import os
import re
from collections import Counter

SRC_DIR = os.path.join(os.path.dirname(__file__), "v0.2")
DST_DIR = os.path.join(os.path.dirname(__file__), "v0.3")

# ---------------------------------------------------------------------------
# Row classification heuristics
# ---------------------------------------------------------------------------

ROUNDUP_RE = re.compile(
    r"\b(among|gainers|losers|movers|most active|watchlist|watch list|"
    r"top \d+|biggest|on the move)\b", re.I)

OFFICER_RE_TEMPLATE = (
    r"\b(?:CEO|CFO|COO|CTO|CIO|President|Chairman|Chairwoman|Founder|"
    r"co-founder|Director)\s+((?:[A-Z][a-z'\-]+\s+){{1,2}})(?i:{key})\b")
# capitalized non-name words that can sit between an officer title and a
# company key ("Enjoy Life Foods CEO Says Tribune ...")
NAME_PART_STOP = {"says", "said", "new", "former", "applauds"}

TICKER_RE = re.compile(r"\(([A-Z]{1,5})\)")

NAME_STOP = {"inc", "corp", "corporation", "group", "co", "company", "ltd",
             "llc", "lp", "reit", "holdings", "holding", "the", "plc", "sa",
             "nv", "trust", "partners", "acquisition"}

# Generic industry/geography words: a company key made ONLY of these cannot
# reliably identify a company in a headline ("Global Healthcare").
GENERIC_WORDS = {
    "global", "international", "american", "united", "national", "general",
    "first", "pacific", "atlantic", "central", "western", "eastern",
    "healthcare", "health", "energy", "resources", "industries", "systems",
    "technologies", "technology", "solutions", "services", "financial",
    "capital", "partners", "properties", "medical", "pharma", "bio",
    "therapeutics", "sciences", "materials", "mining", "oil", "gas",
    "power", "foods", "brands", "products", "group", "ventures",
}


def key_tokens(name):
    """Distinctive name tokens for headline matching.

    Keeps len>=4 lowercase tokens, plus short tokens that are distinctive by
    form: contains a digit ("A123") or is an all-caps acronym in the original
    name ("MF", "RCS", "XXI"). Short plain words are dropped so "On
    Semiconductor" doesn't key on "on".
    """
    if not name:
        return []
    name = re.sub(r"\([^)]*\)", " ", name)  # drop "(GBCS)" ticker annotations
    out = []
    for raw in re.findall(r"[A-Za-z0-9]+", name):
        tok = raw.lower()
        if tok in NAME_STOP or len(tok) < 2:
            continue
        if len(tok) >= 4 or any(c.isdigit() for c in raw) or \
                (raw.isupper() and len(raw) >= 2):
            out.append(tok)
    return out


def classify_row(title, kts, symbol, generic_name):
    """Return noise_kind (None = company-centric row)."""
    tl = title.lower()
    tickers = set(TICKER_RE.findall(title))
    own_ticker = bool(symbol) and symbol.upper() in tickers
    head = tl[:60]
    name_in_head = bool(kts) and kts[0] in head and (
        len(kts) == 1 or kts[1] in head)

    if tickers and not own_ticker and not name_in_head:
        return "foreign_subject"
    if not own_ticker:
        for kt in kts:
            m = re.search(OFFICER_RE_TEMPLATE.format(key=re.escape(kt)),
                          title)
            if m and not any(w.lower() in NAME_PART_STOP
                             for w in m.group(1).split()):
                return "person_mention"
    if ROUNDUP_RE.search(title):
        return "roundup"
    if generic_name and not own_ticker and any(k in tl for k in kts):
        # name matched only via a generic phrase, no ticker evidence
        return "generic_ctx"
    return None


# ---------------------------------------------------------------------------

def annotate_sample(s):
    name = s["company"].get("name")
    symbol = s["company"].get("symbol")
    kts = key_tokens(name)
    generic_name = bool(kts) and all(k in GENERIC_WORDS for k in kts)

    counts = Counter()
    for row in s["news"]:
        kind = classify_row(row["title"], kts, symbol, generic_name)
        row["noise_kind"] = kind
        counts[kind or "centric"] += 1

    n = len(s["news"])
    centric = counts["centric"]
    noise_share = (n - centric) / n if n else 0.0
    flags = []
    if generic_name:
        flags.append("generic_name")
    if noise_share > 0.5:
        flags.append("noisy")
    if centric < 15:
        flags.append("thin_centric")
    s["news_quality"] = {
        "rows": n,
        "centric_rows": centric,
        "noise_share": round(noise_share, 3),
        "by_kind": {k: v for k, v in sorted(counts.items()) if k != "centric"},
        "flags": flags,
    }
    return s


def main():
    with open(os.path.join(SRC_DIR, "samples.jsonl"), encoding="utf-8") as f:
        src_samples = [json.loads(line) for line in f]

    samples = [annotate_sample(json.loads(json.dumps(s))) for s in src_samples]

    # integrity: same samples, same rows, same splits as v0.2
    for a, b in zip(src_samples, samples):
        assert a["sample_id"] == b["sample_id"] and a["split"] == b["split"]
        assert [r["news_id"] for r in a["news"]] == \
               [r["news_id"] for r in b["news"]]

    os.makedirs(DST_DIR, exist_ok=True)
    splits = Counter()
    with open(os.path.join(DST_DIR, "samples.jsonl"), "w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
            splits[s["split"]] += 1
    for split in ("train", "dev", "test"):
        with open(os.path.join(DST_DIR, f"{split}.jsonl"), "w",
                  encoding="utf-8") as f:
            for s in samples:
                if s["split"] == split:
                    f.write(json.dumps(s, ensure_ascii=False) + "\n")

    kind_totals = Counter()
    flagged = []
    for s in samples:
        kind_totals.update(s["news_quality"]["by_kind"])
        if s["news_quality"]["flags"]:
            flagged.append({
                "sample_id": s["sample_id"], "type": s["type"],
                "split": s["split"],
                "company": s["company"].get("name") or s["company"].get("symbol"),
                "centric_rows": s["news_quality"]["centric_rows"],
                "noise_share": s["news_quality"]["noise_share"],
                "flags": s["news_quality"]["flags"],
            })
    flagged.sort(key=lambda x: -x["noise_share"])

    total_rows = sum(len(s["news"]) for s in samples)
    manifest = {
        "version": "0.3",
        "base": "v0.2 (annotation only — identical samples/rows/splits)",
        "samples": len(samples),
        "splits": dict(splits),
        "news_rows": total_rows,
        "noise_rows": sum(kind_totals.values()),
        "noise_by_kind": dict(kind_totals),
        "flagged_samples": flagged,
    }
    with open(os.path.join(DST_DIR, "manifest.json"), "w",
              encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"samples: {len(samples)}  rows: {total_rows}  "
          f"noise rows: {sum(kind_totals.values())} "
          f"({sum(kind_totals.values()) / total_rows:.1%})")
    print("noise by kind:", dict(kind_totals))
    print(f"\nflagged samples: {len(flagged)}")
    for x in flagged:
        print(f"  {x['noise_share']:5.0%}  {x['sample_id']:32} "
              f"{x['type']:9} {x['split']:6} {','.join(x['flags']):28} "
              f"{str(x['company'])[:35]}")


if __name__ == "__main__":
    main()
