"""R08: move a positive's event_date back to when the bankruptcy became public.

The event_date came from EDGAR: the date of the first 8-K carrying Item
1.03. For some companies that 8-K came months after the petition — BurgerFi
filed on 2024-09-11 and its Item 1.03 8-K is dated 2025-02-10 — so the
window "before the event" contained the news of the filing itself, and a
model that read "BurgerFi files for bankruptcy" and said critical was
scored as five months of foresight.

For each positive sample the event becomes the earliest of:
  - the EDGAR Item 1.03 date it already had,
  - the first 8-K in the window whose items include 1.03,
  - the first headline that reports a filing as a fact (files / filed /
    initiates / announces Chapter 11 or 7, seeks bankruptcy protection),
    not one that warns of it (may, might, could, faces, warns, prepares…).
The window moves with it — [event-180, event-1] — and news/filings outside
it are dropped (they are a subset of what was fetched; nothing is fetched).
news_quality is recomputed (R06) since the rows changed. The original date
is kept as label.event_date_original.

Usage: python projects/credit_risk/dataset/builders/build_r08_event_dates.py [--report]
"""

import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_r06_news_quality import assess  # noqa: E402
from build_r04_assemble import assign_splits  # noqa: E402

CONTEMPORARY = HERE / "contemporary"
FILES = ("samples.jsonl", "dev.jsonl", "test.jsonl")
WINDOW_DAYS = 180

FILED = re.compile(
    r"\b(files?|filed|filing|initiates?|initiated|announces?|announced|enters?|entered|"
    r"commences?|commenced|declares?|declared|plummets .* announces)\b"
    r"[^.]{0,60}?\b(bankruptcy|chapter 11|chapter 7|chapter-11|receivership|insolvency)"
    r"|\bseeks? bankruptcy protection\b", re.I)
# Intent, speculation and avoidance are not a filing: "to file", "expected
# to file", "seeks financing to avoid bankruptcy".
WARNING = re.compile(
    r"\b(may|might|could|possible|possibly|potential|faces|facing|risks?|warns?|warning|"
    r"prepares?|preparing|threat|looms?|looming|nears?|weighs?|considers?|considering|"
    r"explores?|exploring|talks|reportedly|sources|would|if|to file|expected|avoid|avert|"
    r"stave off|\?)\b", re.I)


def filing_headline(title: str) -> bool:
    t = title or ""
    return bool(FILED.search(t)) and not WARNING.search(t)


def earliest_public(sample: dict):
    """(date, source, evidence) of the earliest sign the filing had happened."""
    ev = sample["label"]["event_date"]
    best = (ev, "edgar_item_1.03", None)
    for f in sample.get("filings") or []:
        if "1.03" in (f.get("items") or []) and f.get("filing_date") and f["filing_date"] < best[0]:
            best = (f["filing_date"], "8-K item 1.03 in window", f"8-K {f['filing_date']}")
    for n in sorted(sample.get("news") or [], key=lambda n: n.get("date") or ""):
        if n.get("date") and n["date"] < best[0] and filing_headline(n.get("title")):
            best = (n["date"], "news reports filing", n["title"][:90])
            break
    return best


def rewindow(sample: dict, new_event: str, source: str, evidence) -> dict:
    old = sample["label"]["event_date"]
    end = date.fromisoformat(new_event) - timedelta(days=1)
    start = end - timedelta(days=WINDOW_DAYS - 1)
    s, e = start.isoformat(), end.isoformat()
    before = (len(sample["news"]), len(sample.get("filings") or []))
    sample["news"] = [n for n in sample["news"] if s <= (n.get("date") or "") <= e]
    sample["filings"] = [f for f in sample.get("filings") or [] if s <= (f.get("filing_date") or "") <= e]
    sample["window"] = {"start": s, "end": e}
    sample["label"] = {**sample["label"], "event_date": new_event,
                       "event_date_original": old, "event_date_source": source,
                       "event_date_evidence": evidence}
    sample["news_quality"] = assess(sample)
    return {"before": before, "after": (len(sample["news"]), len(sample["filings"]))}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args(argv)
    path = CONTEMPORARY / "samples.jsonl"
    samples = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]

    changes = []
    print(f"{'company':22} {'split':4} {'event':10} {'->':2} {'public':10} {'lag':>5}  evidence")
    for s in samples:
        if s["type"] != "positive":
            continue
        new, source, evidence = earliest_public(s)
        old = s["label"]["event_date"]
        name = (s["company"].get("query_name") or s["company"]["name"])[:22]
        if new < old:
            lag = (date.fromisoformat(old) - date.fromisoformat(new)).days
            print(f"{name:22} {s.get('split', ''):4} {old:10} -> {new:10} {lag:4d}d  {source}: {evidence}")
            changes.append((s, new, source, evidence))
        else:
            print(f"{name:22} {s.get('split', ''):4} {old:10}    (kept)")
    if args.report:
        print(f"\n{len(changes)} would change")
        return 0

    for s, new, source, evidence in changes:
        stats = rewindow(s, new, source, evidence)
        print(f"  rewindowed {s['sample_id']}: news {stats['before'][0]}->{stats['after'][0]}, "
              f"filings {stats['before'][1]}->{stats['after'][1]}, flags {s['news_quality']['flags']}")
    # An earlier event can move a sample across the chronological split line.
    assign_splits(samples)
    path.write_text("".join(json.dumps(s, ensure_ascii=False) + "\n" for s in samples), encoding="utf-8")
    for split in ("dev", "test"):
        (CONTEMPORARY / f"{split}.jsonl").write_text(
            "".join(json.dumps(s, ensure_ascii=False) + "\n" for s in samples if s["split"] == split),
            encoding="utf-8")
    manifest_path = CONTEMPORARY / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["event_dates"] = {
        "rule": "R08: earliest of EDGAR Item 1.03 date, an in-window 8-K with item 1.03, "
                "or the first headline reporting a filing as fact; window moved with it",
        "changed": [{"sample_id": s["sample_id"], "from": s["label"]["event_date_original"],
                     "to": s["label"]["event_date"], "source": s["label"]["event_date_source"]}
                    for s, *_ in changes],
    }
    from collections import Counter
    after = Counter((s["split"], s["type"]) for s in samples)
    manifest["splits"] = {f"{t[:3]}_{sp}": n for (sp, t), n in sorted(after.items())}
    manifest["news_quality"]["flags"] = {s["sample_id"]: s["news_quality"]["flags"]
                                         for s in samples if s.get("news_quality", {}).get("flags")}
    manifest["news_rows"] = sum(len(s["news"]) for s in samples)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n{len(changes)} samples re-dated; splits now {dict(sorted(after.items()))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
