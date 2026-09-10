"""R06: annotate contemporary samples with `news_quality` (annotation only).

The v0.3 idea, applied to the contemporary set: a news row matched by
company *name* is not necessarily *about* the company. "Silver Star" pulled
in hit-and-runs on Silver Star Road and deaths in Silver Star Provincial
Park — 28 rows, none about the REIT — and the pipeline was scored on
missing a bankruptcy no headline mentioned. A sample like that is flagged
here so metrics can be reported with and without it; nothing is removed.

Per row nothing is stored; per sample:
  news_quality = {rows, centric_rows, noise_share, flags}
  flags ⊆ {generic_name, noisy, thin_centric}

A row is *centric* when the title (or the head of its text) carries the
ticker, the company's full core name, or the query name next to a corporate
word (earnings, shares, CEO, bankruptcy, …). Heuristic, like v0.3: expect a
few percent error in both directions; the flags are what matter.

Usage: python projects/credit_risk/dataset/builders/build_r06_news_quality.py [--report]
  --report prints the per-sample table and writes nothing.
"""

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
CONTEMPORARY = HERE / "contemporary"
FILES = ("samples.jsonl", "train.jsonl", "dev.jsonl", "test.jsonl")

SUFFIXES = {"inc", "inc.", "corp", "corp.", "corporation", "co", "co.", "ltd",
            "ltd.", "llc", "plc", "holdings", "holding", "group", "company",
            "the", "&", "and"}
CORPORATE = {
    "inc", "corp", "company", "shares", "stock", "nasdaq", "nyse", "ceo",
    "cfo", "chairman", "earnings", "revenue", "quarter", "q1", "q2", "q3", "q4",
    "fiscal", "guidance", "bankruptcy", "chapter", "filing", "8-k", "sec",
    "debt", "lender", "lenders", "investors", "investor", "analyst", "analysts",
    "dividend", "acquisition", "acquire", "merger", "layoffs", "restructuring",
    "going-concern", "concern", "default", "creditors", "notes", "bonds",
    "rating", "downgrade", "upgrade", "ipo", "delisting", "delist", "spac",
    "results", "loss", "profit", "sales", "outlook", "price target", "reit",
}
# Words that name the world, not a company. A query name made only of these
# matches everything with the words in it.
GENERIC = {
    "silver", "star", "global", "health", "healthcare", "care", "energy",
    "american", "america", "national", "united", "general", "first", "capital",
    "properties", "property", "international", "digital", "bright", "green",
    "street", "broad", "blue", "red", "gold", "sun", "sky", "north", "south",
    "east", "west", "new", "home", "city", "tech", "technology", "power",
    "air", "water", "bio", "life", "one", "light", "wave", "spirit", "royal",
    "pacific", "atlantic", "central", "premier", "prime", "standard", "modern",
    "smart", "real", "estate", "trust", "partners", "brands", "foods", "media",
    "auto", "parts", "motors", "systems", "solutions", "services", "network",
}

NOISY_SHARE = 0.5
THIN_CENTRIC = 10


def _normalise(text: str) -> str:
    """GDELT's rendering, undone: "Wendy 's"/"Wendy" for Wendy's, "Li - Cycle"
    for Li-Cycle. Possessives are dropped on both sides, spaced hyphens joined."""
    text = text.lower()
    text = re.sub(r"\s*-\s*", "-", text)
    text = re.sub(r"\s*'\s*s\b", "", text)
    return text


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9][a-z0-9&.-]*", _normalise(text))


def _core_name(name: str) -> list[str]:
    name = re.sub(r"\(.*?\)", " ", name or "")
    toks = [t.strip(".,") for t in _tokens(name)]
    return [t for t in toks if t and t not in SUFFIXES]


def _is_centric(row: dict, symbol: str | None, core: list[str], query: list[str]) -> bool:
    head = f"{row.get('title') or ''} {(row.get('text') or '')[:300]}"
    low = head.lower()
    if symbol and re.search(rf"\b{re.escape(symbol)}\b", head):
        return True
    if core and " ".join(core) in " ".join(_tokens(low)):
        return True
    if query and " ".join(query) in " ".join(_tokens(low)):
        words = set(_tokens(low))
        if words & CORPORATE or "price target" in low or "chapter 11" in low:
            return True
    return False


def assess(sample: dict) -> dict:
    company = sample.get("company") or {}
    symbol = (company.get("symbol") or "").split(",")[0].strip() or None
    core = _core_name(company.get("name") or "")
    query = _core_name(company.get("query_name") or company.get("name") or "")
    rows = sample.get("news") or []
    centric = sum(_is_centric(r, symbol, core, query) for r in rows)
    share = round(1 - centric / len(rows), 3) if rows else 0.0
    flags = []
    if query and all(t in GENERIC for t in query):
        flags.append("generic_name")
    if share > NOISY_SHARE:
        flags.append("noisy")
    if centric < THIN_CENTRIC:
        flags.append("thin_centric")
    return {"rows": len(rows), "centric_rows": centric, "noise_share": share,
            "flags": flags}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args(argv)

    quality = {}
    samples = [json.loads(l) for l in (CONTEMPORARY / "samples.jsonl").open(encoding="utf-8")]
    for s in samples:
        quality[s["sample_id"]] = assess(s)

    print(f"{'sample':30} {'type':8} {'split':5} rows centric noise  flags")
    for s in samples:
        q = quality[s["sample_id"]]
        print(f"{s['sample_id']:30} {s['type']:8} {s.get('split',''):5} "
              f"{q['rows']:4d} {q['centric_rows']:7d} {q['noise_share']:5.2f}  {','.join(q['flags'])}")
    flagged = sorted(sid for sid, q in quality.items() if q["flags"])
    print(f"\nflagged: {len(flagged)} of {len(samples)}: {flagged}")
    if args.report:
        return 0

    for name in FILES:
        path = CONTEMPORARY / name
        lines = path.read_text(encoding="utf-8").splitlines()
        out = []
        for line in lines:
            if not line.strip():
                continue
            s = json.loads(line)
            s["news_quality"] = quality[s["sample_id"]]
            out.append(json.dumps(s, ensure_ascii=False))
        assert len(out) == len([l for l in lines if l.strip()])
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
    manifest_path = CONTEMPORARY / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["news_quality"] = {
        "method": "R06 heuristic: ticker / full core name / query name + corporate word",
        "flags": {sid: quality[sid]["flags"] for sid in flagged},
        "thresholds": {"noisy": NOISY_SHARE, "thin_centric": THIN_CENTRIC},
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote news_quality into {', '.join(FILES)} and manifest.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
