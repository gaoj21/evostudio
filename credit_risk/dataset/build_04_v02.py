"""
Step 04: build dataset v0.2 from v0.1 samples (pure post-processing, no
network and no re-scan of All_external.csv).

Adds two things on top of v0.1:

1. `dup_cluster_id` on every news row — near-duplicate clustering so the
   dedup behavior of a memory layer can be evaluated. FNSPID aggregates the
   same wire story from multiple publishers, often with light edits
   ("UPDATE 1-...", corrected headlines, re-dated reposts).
   Method, per sample:
     - normalize title (lowercase, alnum-only tokens, collapse spaces);
     - union-find over rows: two rows merge when their dates are within
       DUP_DATE_TOLERANCE days AND token-set Jaccard >= DUP_JACCARD_MIN
       (exact normalized-title equality always merges);
     - cluster id is sample-scoped: "c000", "c001", ... ordered by the
       cluster's earliest date. Singletons get their own id (every row has
       a dup_cluster_id; a cluster of size 1 simply means "no duplicate").

2. Official train / dev / test split, by time AND company:
     - WITHIN each class (positive / negative) samples are sorted by
       window.end and cut at ~70% / ~15% / ~15%. Per-class cuts are used
       instead of one global date cut because bankruptcies cluster in
       crisis years (2008-09, 2020): a global cut leaves dev with almost
       no positives. Every split still spans the full era, ordered
       past -> future inside each class;
     - a company (CIK for positives, symbol for negatives) may appear in
       only one split — enforced and asserted (in v0.1 positives are already
       one-per-CIK after event dedup and negatives one-per-symbol, so this
       is a guarantee check, not a repair pass);
     - the split label is stored on each sample as "split".

Output: v0.2/samples.jsonl (everything, annotated), v0.2/train.jsonl,
v0.2/dev.jsonl, v0.2/test.jsonl, v0.2/manifest.json.
"""

import json
import re
from collections import Counter, defaultdict
from datetime import datetime

IN_SAMPLES = "credit_risk/dataset/v0.1/samples.jsonl"
OUT_DIR = "credit_risk/dataset/v0.2"

DUP_JACCARD_MIN = 0.90      # token-set Jaccard for fuzzy duplicate merge
DUP_DATE_TOLERANCE = 3      # days; wire reposts land within a few days
TRAIN_FRAC, DEV_FRAC = 0.70, 0.15  # test = remainder

# wire-service prefixes stripped before comparison: "UPDATE 2-...",
# "RPT-WRAPUP 4-...", "CORRECTED-..." are the same story re-issued
WIRE_PREFIXES = {"update", "corrected", "rpt", "wrapup", "exclusive",
                 "bulletin", "alert", "correct"}


def normalize_title(title: str) -> str:
    text = title.lower()
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    tokens = re.sub(r"\s+", " ", text).strip().split()
    while tokens and tokens[0] in WIRE_PREFIXES:
        tokens.pop(0)
        if tokens and tokens[0].isdigit():
            tokens.pop(0)  # the "1" in "UPDATE 1-..."
    return " ".join(tokens)


def parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d")


class UnionFind:
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def cluster_news(news):
    """Assign dup_cluster_id to each row of one sample's news list."""
    n = len(news)
    uf = UnionFind(n)
    norms = [normalize_title(r["title"]) for r in news]
    tokens = [set(t.split()) for t in norms]
    dates = [parse_date(r["date"]) for r in news]

    # exact normalized-title buckets always merge
    by_norm = defaultdict(list)
    for i, t in enumerate(norms):
        by_norm[t].append(i)
    for idxs in by_norm.values():
        for j in idxs[1:]:
            uf.union(idxs[0], j)

    # fuzzy merge: only compare rows close in date (news is date-sorted,
    # so a sliding window over indices bounds the pair count)
    order = sorted(range(n), key=lambda i: dates[i])
    for pos, i in enumerate(order):
        j_pos = pos + 1
        while j_pos < n:
            j = order[j_pos]
            if (dates[j] - dates[i]).days > DUP_DATE_TOLERANCE:
                break
            a, b = tokens[i], tokens[j]
            if a and b:
                inter = len(a & b)
                if inter / len(a | b) >= DUP_JACCARD_MIN:
                    uf.union(i, j)
            j_pos += 1

    # name clusters by their earliest date, then smallest index
    clusters = defaultdict(list)
    for i in range(n):
        clusters[uf.find(i)].append(i)
    ordered = sorted(clusters.values(),
                     key=lambda idxs: (min(dates[i] for i in idxs), min(idxs)))
    for k, idxs in enumerate(ordered):
        for i in idxs:
            news[i]["dup_cluster_id"] = f"c{k:03d}"
    return Counter(len(v) for v in clusters.values())


def company_key_of(sample):
    c = sample["company"]
    return c["cik"] if c["cik"] is not None else f"sym:{c['symbol']}"


def split_samples(samples):
    """Per-class time-ordered 70/15/15 split; no company crosses splits."""
    for cls in ("positive", "negative"):
        subset = sorted((s for s in samples if s["type"] == cls),
                        key=lambda s: s["window"]["end"])
        n = len(subset)
        train_end = int(n * TRAIN_FRAC)
        dev_end = int(n * (TRAIN_FRAC + DEV_FRAC))
        for i, s in enumerate(subset):
            s["split"] = ("train" if i < train_end
                          else "dev" if i < dev_end else "test")

    seen = {}
    for s in samples:
        key = company_key_of(s)
        if key in seen:
            assert seen[key] == s["split"], (
                f"company {key} in both {seen[key]} and {s['split']}")
        seen[key] = s["split"]
    return samples


def split_stats(samples):
    stats = {}
    for split in ("train", "dev", "test"):
        subset = [s for s in samples if s["split"] == split]
        pos = [s for s in subset if s["type"] == "positive"]
        stats[split] = {
            "n_samples": len(subset),
            "n_positive": len(pos),
            "n_negative": len(subset) - len(pos),
            "n_news": sum(len(s["news"]) for s in subset),
            "window_end_range": [
                min(s["window"]["end"] for s in subset),
                max(s["window"]["end"] for s in subset),
            ] if subset else None,
        }
    return stats


def main():
    import os
    os.makedirs(OUT_DIR, exist_ok=True)

    with open(IN_SAMPLES, encoding="utf-8") as f:
        samples = [json.loads(line) for line in f]
    print(f"loaded {len(samples)} samples from {IN_SAMPLES}")

    # 1) duplicate clustering
    size_hist = Counter()
    n_rows = 0
    for s in samples:
        hist = cluster_news(s["news"])
        size_hist.update(hist)
        n_rows += len(s["news"])
    n_clusters = sum(
        len({r["dup_cluster_id"] for r in s["news"]}) for s in samples)
    n_dup_rows = n_rows - n_clusters  # rows beyond the first in each cluster
    print(f"dup clustering: {n_rows} rows -> {n_clusters} clusters "
          f"({n_dup_rows} duplicate rows, {n_dup_rows / n_rows:.1%})")
    print(f"  cluster size histogram: "
          + ", ".join(f"size {k}: {v}" for k, v in sorted(size_hist.items())))

    # 2) splits
    samples = split_samples(samples)
    stats = split_stats(samples)
    for split, st in stats.items():
        print(f"  {split}: {st['n_samples']} samples "
              f"({st['n_positive']} pos / {st['n_negative']} neg), "
              f"window.end {st['window_end_range'][0]} .. "
              f"{st['window_end_range'][1]}")

    # leakage re-check: no news on/after the event date
    for s in samples:
        assert all(r["date"] <= s["window"]["end"] for r in s["news"]), \
            f"leakage in {s['sample_id']}"

    # write outputs
    with open(f"{OUT_DIR}/samples.jsonl", "w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    for split in ("train", "dev", "test"):
        with open(f"{OUT_DIR}/{split}.jsonl", "w", encoding="utf-8") as f:
            for s in samples:
                if s["split"] == split:
                    f.write(json.dumps(s, ensure_ascii=False) + "\n")

    manifest = {
        "version": "0.2",
        "base": "v0.1 samples + dup_cluster_id annotations + official splits",
        "window_days": 180,
        "n_samples": len(samples),
        "n_positive": sum(1 for s in samples if s["type"] == "positive"),
        "n_negative": sum(1 for s in samples if s["type"] == "negative"),
        "total_news": n_rows,
        "dedup": {
            "method": f"exact normalized-title OR token-Jaccard>={DUP_JACCARD_MIN} "
                      f"within {DUP_DATE_TOLERANCE}d, union-find, per sample",
            "n_clusters": n_clusters,
            "n_duplicate_rows": n_dup_rows,
            "duplicate_row_frac": round(n_dup_rows / n_rows, 4),
            "cluster_size_histogram": {str(k): v for k, v in sorted(size_hist.items())},
        },
        "splits": stats,
        "split_rule": "per-class, by window.end (~70/15/15); no company in two splits",
    }
    with open(f"{OUT_DIR}/manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"wrote {OUT_DIR}/")


if __name__ == "__main__":
    main()
