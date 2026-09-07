#!/usr/bin/env python
"""Replay the contemporary dataset through the agentic alert pipeline.

Each sample is an independent monitoring scenario: its news (GDELT, with
optional full text) and 8-K filings are fed to a FRESH pipeline instance
(per-sample memory store), and the alerts it issues are scored against the
sample's label:

- positive (bankruptcy at ref_date): detected? lead time = event_date -
  first alert date, in days
- negative: any alert is a false alarm

Checkpointing: one JSON per finished sample under the output dir; re-running
skips them. Safe to interrupt and resume.

Run from the repository root:
    .venv/bin/python credit_risk/run_contemporary_eval.py --split dev
    .venv/bin/python credit_risk/run_contemporary_eval.py --split test
    .venv/bin/python credit_risk/run_contemporary_eval.py --split all --workers 4
"""

import argparse
import glob
import json
import os
import sys
import threading
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(os.path.abspath(os.path.dirname(__file__)),
                                "agentic_pipeline"))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..")))  # repo root (llm/)

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from dotenv import load_dotenv  # noqa: E402

from llm import get_evoagentx_llm  # noqa: E402
from agentic_pipeline import (AutoApprover, ObligorRegistry, RawItem,  # noqa: E402
                              build_pipeline, weight_of_evidence)

DATASET_DIR = os.path.join("credit_risk", "dataset", "contemporary")
OUT_DIR = os.path.join("credit_risk", "output", "eval_contemporary")

# build_pipeline mutates demo.STORE_DIR before constructing the memory;
# serialize construction so concurrent samples can't clobber each other.
_build_lock = threading.Lock()


def build_items(sample: dict, with_text: bool) -> list:
    """Sample -> RawItems (news headlines [+ bodies] and 8-K filings)."""
    cik = sample["company"]["cik"]
    name = sample["company"]["name"]
    items = []
    for r in sample["news"]:
        items.append(RawItem(
            source="news", date=r["date"], title=r["title"],
            text=(r.get("text") or None) if with_text else None,
            url=r.get("url"), publisher=r.get("publisher")))
    for f in sample["filings"]:
        items.append(RawItem(
            source="8k", date=f["filing_date"],
            title=f"{name} 8-K items {','.join(f.get('items') or [])}",
            text=f.get("text"), cik=cik, registrant_name=name,
            items=f.get("items") or []))
    items.sort(key=lambda it: it.date)
    return items


def eval_sample(sample: dict, registry, out_dir: str, with_text: bool,
                force: bool) -> dict:
    sid = sample["sample_id"]
    out_path = os.path.join(out_dir, f"{sid}.json")
    if os.path.exists(out_path) and not force:
        return json.load(open(out_path))

    store_dir = os.path.join(out_dir, "stores", sid)
    llm = get_evoagentx_llm()  # default provider from llm/providers.json
    with _build_lock:
        pipeline = build_pipeline(llm, registry, store_dir=store_dir,
                                  fresh=True,
                                  woe_evaluator=weight_of_evidence,
                                  human_feedback=AutoApprover())
    items = build_items(sample, with_text)
    result = pipeline.ingest(items)

    first_alert = min((a["date"] for a in result.alerts), default=None)
    rec = {
        "sample_id": sid,
        "type": sample["type"],
        "company": sample["company"]["name"],
        "window": sample["window"],
        "label": sample["label"],
        "n_items": len(items),
        "n_alerts": len(result.alerts),
        "n_suppressed": len(result.suppressed),
        "n_dropped_unmatched": len(result.dropped_unmatched),
        "first_alert_date": first_alert,
        "alerts": [{"date": a["date"], "source": a["source"],
                    "topic": a["detection"].get("topic"),
                    "severity": a["detection"].get("severity"),
                    "score": a["decision"].get("score"),
                    "woe": a.get("weight_of_evidence")}
                   for a in result.alerts],
    }
    ev = sample["label"].get("event_date")
    if sample["type"] == "positive" and first_alert and ev:
        rec["lead_days"] = (datetime.strptime(ev, "%Y-%m-%d")
                            - datetime.strptime(first_alert,
                                                "%Y-%m-%d")).days
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)
    return rec


def summarize(records: list):
    pos = [r for r in records if r["type"] == "positive"]
    neg = [r for r in records if r["type"] == "negative"]
    det = [r for r in pos if r["n_alerts"] > 0]
    leads = [r["lead_days"] for r in det if r.get("lead_days") is not None]
    fa = [r for r in neg if r["n_alerts"] > 0]
    print(f"\n{'='*60}")
    print(f"positives: {len(det)}/{len(pos)} detected")
    if leads:
        print(f"lead days: mean {sum(leads)/len(leads):.0f}, "
              f"min {min(leads)}, max {max(leads)}")
    print(f"negatives: {len(fa)}/{len(neg)} with alerts (false alarms)")
    for r in fa:
        print(f"  FA: {r['company'][:40]} — {r['n_alerts']} alerts, "
              f"first {r['first_alert_date']}")
    print(f"{'='*60}")
    for r in records:
        tag = "POS" if r["type"] == "positive" else "NEG"
        lead = f"lead={r['lead_days']}d" if r.get("lead_days") is not None \
            else "lead=-"
        print(f"{tag} {r['company'][:38]:38s} alerts={r['n_alerts']:2d} "
              f"supp={r['n_suppressed']:3d} drop={r['n_dropped_unmatched']:2d} "
              f"{lead}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev",
                    choices=["train", "dev", "test", "all"])
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--with-text", action="store_true",
                    help="feed news full text (bodies) instead of titles only")
    ap.add_argument("--force", action="store_true",
                    help="re-run even if a per-sample result exists")
    ap.add_argument("--out", default=OUT_DIR)
    args = ap.parse_args()

    load_dotenv()
    if not os.getenv("DEEPSEEK_API_KEY"):
        raise SystemExit("DEEPSEEK_API_KEY not found.")

    from agentic_pipeline.obligors import ObligorRegistry as Reg
    registry = Reg.from_candidates_csv(
        os.path.join(DATASET_DIR, "candidates.csv"))
    print(f"registry: {len(registry)} obligors")

    path = (os.path.join(DATASET_DIR, "samples.jsonl")
            if args.split == "all"
            else os.path.join(DATASET_DIR, f"{args.split}.jsonl"))
    samples = [json.loads(l) for l in open(path, encoding="utf-8")]
    print(f"{len(samples)} samples ({args.split}), "
          f"workers={args.workers}, with_text={args.with_text}")

    os.makedirs(args.out, exist_ok=True)
    records = []
    if args.workers <= 1:
        for s in samples:
            rec = eval_sample(s, registry, args.out, args.with_text,
                              args.force)
            records.append(rec)
            print(f"done {rec['sample_id']}: alerts={rec['n_alerts']}",
                  flush=True)
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(eval_sample, s, registry, args.out,
                              args.with_text, args.force): s
                    for s in samples}
            for fut in as_completed(futs):
                rec = fut.result()
                records.append(rec)
                print(f"done {rec['sample_id']}: alerts={rec['n_alerts']}",
                      flush=True)

    summarize(records)
    summary_path = os.path.join(args.out, f"summary_{args.split}.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    print(f"\nwrote {summary_path}")


if __name__ == "__main__":
    main()
