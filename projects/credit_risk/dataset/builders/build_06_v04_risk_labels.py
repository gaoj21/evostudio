#!/usr/bin/env python
"""build_06_v04_risk_labels.py — v0.3 -> v0.4: gold risk-type labels per news row.

The pipeline classifies extracted events into the closed credit-risk taxonomy
(`examples/projects/credit_risk/skills/credit_risk_taxonomy`), but the dataset has no
ground truth for that classification, so its quality is unmeasured. v0.4 adds
a `gold_risk_type` field to every news row: one of the 8 taxonomy event types
if the headline describes a credit-risk-relevant event ABOUT the sample's
company, else null.

Method (LLM pre-labeling, cheap and resumable):
- rows with `noise_kind != null` (v0.3 annotation) are auto-labeled null
  without an LLM call — they are not company-relevant by construction
- remaining rows are labeled per (sample, date) batch by deepseek-v4-flash;
  within a batch only one representative per `dup_cluster_id` is sent, and
  its label is propagated to cluster members (15.6% of rows saved)
- per-sample checkpoint in v0.4/labels_ckpt/ — rerun to resume
- samples/windows/splits/row order identical to v0.3 (asserted)

Labels are LLM silver labels, not audited gold: expect single-digit % noise.
A random per-type sample is printed for eyeballing at the end.

Run from the repository root (needs DEEPSEEK_API_KEY in .env):
    .venv/bin/python projects/credit_risk/dataset/builders/build_06_v04_risk_labels.py --selftest
    .venv/bin/python projects/credit_risk/dataset/builders/build_06_v04_risk_labels.py
"""

import argparse
import json
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..", "..")))
from dotenv import load_dotenv  # noqa: E402

from evoagentx.models.litellm_model import LiteLLM, LiteLLMConfig  # noqa: E402

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "v0.3")
DST_DIR = os.path.join(os.path.dirname(__file__), "..", "v0.4")
CKPT_DIR = os.path.join(DST_DIR, "labels_ckpt")
TAXONOMY_MD = os.path.join(os.path.dirname(__file__), "..", "..", "skills",
                           "credit_risk_taxonomy", "SKILL.md")
MODEL = "deepseek/deepseek-v4-flash"
MAX_ROWS_PER_CALL = 40

EVENT_TYPES = {"debt_default", "rating_downgrade", "liquidity_stress",
               "lawsuit_regulatory", "earnings_warning", "management_turmoil",
               "operational_shock", "positive_development"}

PERMANENT = ("insufficient balance", "authentication", "invalid api key",
             "permission", "model not found")


def _retry(fn, attempts=4, wait=15.0):
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as e:
            msg = str(e).lower()
            if any(p in msg for p in PERMANENT):
                raise
            if attempt == attempts - 1:
                raise
            print(f"    retry {attempt + 1}/{attempts} after: "
                  f"{type(e).__name__}: {e}")
            time.sleep(wait * (attempt + 1))


def load_taxonomy():
    with open(TAXONOMY_MD, encoding="utf-8") as f:
        return f.read()


def label_batch(llm, taxonomy, company, rows):
    """rows: list of (idx, date, title). Returns {idx: event_type|None}.

    The labeler sees 1-based positions within the batch; we map them back
    to sample row indices.
    """
    listing = "\n".join(f"{n}. [{d}] {t}"
                        for n, (idx, d, t) in enumerate(rows, 1))
    prompt = f"""You are labeling news headlines for a credit-risk dataset.

Company under watch: {company}

Classify each headline: if it describes a credit-risk-relevant event ABOUT
THIS company, assign exactly one event_type from the taxonomy below. If the
headline is about other companies, is a routine gainers/losers roundup, or
contains no credit-risk-relevant event for this company, use null.

{taxonomy}

Return ONLY a JSON array, one object per headline, same order and count:
[{{"i": 1, "event_type": "liquidity_stress"}}, {{"i": 2, "event_type": null}}, ...]

Headlines:
{listing}"""

    def call_and_parse():
        resp = llm.generate(
            prompt=prompt,
            system_message="You are a precise financial-data labeler. "
                           "Output valid JSON only.")
        m = re.search(r"\[.*\]", resp.content.strip(), re.S)
        if not m:
            raise RuntimeError("no JSON array in labeler output")
        try:
            arr = json.loads(m.group(0))
        except json.JSONDecodeError as e:
            raise RuntimeError(f"malformed labeler JSON: {e}")
        return {int(o["i"]): o.get("event_type") for o in arr
                if isinstance(o, dict) and o.get("i") is not None}

    by_i = _retry(call_and_parse)
    out = {}
    for n, (idx, _, _) in enumerate(rows, 1):
        et = by_i.get(n)
        out[idx] = et if et in EVENT_TYPES else None
    return out


def label_sample(llm, taxonomy, sample):
    """Return {news_id: event_type|None} for every news row."""
    name = sample["company"].get("name") or sample["company"].get("symbol")
    labels = {}
    # noise rows: null without an LLM call; group the rest by date,
    # one representative per dup cluster
    by_date = {}
    seen_clusters = set()
    propagate = {}  # news_id -> rep news_id
    for idx, row in enumerate(sample["news"]):
        if row.get("noise_kind"):
            labels[row["news_id"]] = None
            continue
        cid = row.get("dup_cluster_id")
        if cid is not None and cid in seen_clusters:
            continue  # labeled via its representative
        if cid is not None:
            seen_clusters.add(cid)
        by_date.setdefault(row["date"], []).append((idx, row["date"],
                                                    row["title"]))
        for j, r2 in enumerate(sample["news"]):
            if r2 is row or r2.get("noise_kind"):
                continue
            if cid is not None and r2.get("dup_cluster_id") == cid \
                    and r2["news_id"] != row["news_id"]:
                propagate[r2["news_id"]] = row["news_id"]

    work = []
    for date in sorted(by_date):
        rows = by_date[date]
        for k in range(0, len(rows), MAX_ROWS_PER_CALL):
            work.append(rows[k:k + MAX_ROWS_PER_CALL])
    for rows in work:
        batch_labels = label_batch(llm, taxonomy, name, rows)
        for idx, et in batch_labels.items():
            labels[sample["news"][idx]["news_id"]] = et
    for nid, rep in propagate.items():
        labels[nid] = labels.get(rep)
    return labels


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true",
                    help="label one sample and print, write nothing")
    ap.add_argument("--threads", type=int, default=5)
    args = ap.parse_args()

    load_dotenv()
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise SystemExit("DEEPSEEK_API_KEY not found.")
    llm = LiteLLM(config=LiteLLMConfig(model=MODEL, deepseek_key=api_key,
                                       timeout=120))
    taxonomy = load_taxonomy()

    with open(os.path.join(SRC_DIR, "samples.jsonl"), encoding="utf-8") as f:
        samples = [json.loads(line) for line in f]

    if args.selftest:
        s = next(x for x in samples
                 if x["sample_id"].startswith("pos_1293310"))
        labels = label_sample(llm, taxonomy, s)
        print(f"selftest on {s['sample_id']} ({s['company']['name']})")
        for row in s["news"]:
            et = labels[row["news_id"]]
            if et:
                print(f"  {row['date']}  {et:22} {row['title'][:80]}")
        from collections import Counter
        print(Counter(labels.values()))
        return

    # balance precheck — a dead key would otherwise burn hours of retries
    import urllib.request
    req = urllib.request.Request(
        "https://api.deepseek.com/user/balance",
        headers={"Authorization": f"Bearer {api_key}"})
    info = json.loads(urllib.request.urlopen(req, timeout=15).read())
    balances = info.get("balance_infos", [])
    print(f"DeepSeek balance: {balances}")
    if not any(float(b.get("total_balance", 0)) > 0 for b in balances):
        raise SystemExit("DeepSeek account has no usable balance — top up.")

    os.makedirs(CKPT_DIR, exist_ok=True)

    def work(sample):
        ckpt = os.path.join(CKPT_DIR, f"{sample['sample_id']}.json")
        if os.path.exists(ckpt):
            with open(ckpt, encoding="utf-8") as f:
                return sample["sample_id"], json.load(f)
        labels = label_sample(llm, taxonomy, sample)
        with open(ckpt, "w", encoding="utf-8") as f:
            json.dump(labels, f)
        return sample["sample_id"], labels

    all_labels = {}
    done = 0
    with ThreadPoolExecutor(max_workers=args.threads) as ex:
        futs = {ex.submit(work, s): s["sample_id"] for s in samples}
        for fut in as_completed(futs):
            sid = futs[fut]
            try:
                sid, labels = fut.result()
                all_labels[sid] = labels
                done += 1
                if done % 20 == 0:
                    print(f"  [{done}/{len(samples)}] labeled")
            except Exception as e:
                raise SystemExit(
                    f"labeling failed on {sid}: {type(e).__name__}: {e} — "
                    f"fix and rerun (checkpoints resume)")

    from collections import Counter
    dist = Counter()
    out_samples = []
    for s in samples:
        labels = all_labels[s["sample_id"]]
        for row in s["news"]:
            et = labels.get(row["news_id"])
            row["gold_risk_type"] = et
            dist[et or "null"] += 1
        out_samples.append(s)

    # integrity vs v0.3 (row identity/order; v0.3 rows gained gold_risk_type)
    with open(os.path.join(SRC_DIR, "samples.jsonl"), encoding="utf-8") as f:
        for a in map(json.loads, f):
            b = next(x for x in out_samples
                     if x["sample_id"] == a["sample_id"])
            assert a["split"] == b["split"]
            assert [r["news_id"] for r in a["news"]] == \
                   [r["news_id"] for r in b["news"]]

    os.makedirs(DST_DIR, exist_ok=True)
    with open(os.path.join(DST_DIR, "samples.jsonl"), "w",
              encoding="utf-8") as f:
        for s in out_samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    for split in ("train", "dev", "test"):
        with open(os.path.join(DST_DIR, f"{split}.jsonl"), "w",
                  encoding="utf-8") as f:
            for s in out_samples:
                if s["split"] == split:
                    f.write(json.dumps(s, ensure_ascii=False) + "\n")

    n_rows = sum(dist.values())
    manifest = {
        "version": "0.4",
        "base": "v0.3 + gold_risk_type per news row (LLM silver labels)",
        "label_model": MODEL,
        "label_method": "per (sample, date) batch; dup-cluster propagation; "
                        "noise_kind rows auto-null",
        "samples": len(out_samples),
        "news_rows": n_rows,
        "gold_risk_type_distribution": dict(dist.most_common()),
        "labeled_frac": round(1 - dist["null"] / n_rows, 4),
    }
    with open(os.path.join(DST_DIR, "manifest.json"), "w",
              encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"\nrows: {n_rows}, labeled (non-null): {n_rows - dist['null']} "
          f"({1 - dist['null'] / n_rows:.1%})")
    for k, v in dist.most_common():
        print(f"  {k:24} {v}")

    print("\nrandom spot-check (5 per type):")
    random.seed(0)
    by_type = {}
    for s in out_samples:
        for row in s["news"]:
            if row["gold_risk_type"]:
                by_type.setdefault(row["gold_risk_type"], []).append(
                    (s["sample_id"], row))
    for et, rows in sorted(by_type.items()):
        print(f"  == {et} ({len(rows)})")
        for sid, row in random.sample(rows, min(5, len(rows))):
            print(f"    {sid[:20]:22} {row['date']} {row['title'][:75]}")


if __name__ == "__main__":
    main()
