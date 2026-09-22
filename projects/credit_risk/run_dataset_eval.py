"""
Evaluate the credit-risk pipeline (credit_risk_demo.py) on the
credit_risk_dataset (projects/credit_risk/dataset/v0.2).

For each sample (one company time-window) the news is replayed day by day,
exactly like the demo's daily-crawl loop: dedup against the sample's news
archive -> stage survivors into short-term memory -> 3-node workflow with the
long-term profile -> profile curation + reflection.

Two modes:

- Full mode: runs the LLM workflow (needs DEEPSEEK_API_KEY) and scores the
  per-day verdicts against the labels:
    * positives: does risk reach high/critical before the bankruptcy, and how
      early (lead time = event_date - first day flagged high/critical)?
    * negatives: does risk stay low/medium (false-alarm rate)?
    * trajectory: per-day predicted risk_level vs. the dataset's derived
      risk bands (days_to_event -> critical/high/medium/low).
- --dedup-only: no LLM at all. Runs only the news-dedup stage (local bge
  embeddings) and scores it against the dataset's dup_cluster_id ground
  truth: an incoming item is a true duplicate iff its cluster was already
  seen in that sample's stream.

Usage (from repo root):
    python examples/projects/credit_risk/run_dataset_eval.py --dedup-only
    python examples/projects/credit_risk/run_dataset_eval.py --n-samples 2 --max-days 5
    python examples/projects/credit_risk/run_dataset_eval.py --input projects/credit_risk/dataset/v0.2/dev.jsonl
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..")))  # repo root (llm/)

# Reuse the demo's pipeline (same directory; script dir is on sys.path).
from credit_risk_demo import (
    SKILLS_DIR,
    AgentManager,
    LiteLLM,
    LiteLLMConfig,
    ShortTermMemory,
    SkillManager,
    WorkFlow,
    archive_news,
    assess,
    build_graph,
    build_memory,
    dedup_news,
    recall_profile,
    reflect,
    save_profile,
    seed_case_library,
    stage_news,
    update_profile,
)

OUT_DIR = os.path.join("projects", "credit_risk", "output", "dataset_eval")

RISK_BANDS = [  # (max days_to_event, derived label) — mirrors the dataset README
    (30, "critical"),
    (90, "high"),
    (180, "medium"),
]
LEVEL_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def derived_risk_level(sample: dict, date: str) -> str:
    """The dataset's per-day risk band for a given news date."""
    if sample["type"] != "positive":
        return "low"
    event = datetime.strptime(sample["label"]["event_date"], "%Y-%m-%d")
    days = (event - datetime.strptime(date, "%Y-%m-%d")).days
    for max_days, level in RISK_BANDS:
        if days <= max_days:
            return level
    return "low"


def group_by_date(sample: dict):
    """news rows -> [(date, [item_text, ...]), ...] in date order, with the
    dataset row kept alongside for cluster ground truth."""
    by_date = defaultdict(list)
    for row in sample["news"]:
        by_date[row["date"]].append(row)
    return sorted(by_date.items())


def item_text(row: dict) -> str:
    return f"{row['date']} | {row['title']}"


# ---------------------------------------------------------------------------
# Dedup evaluation (offline — no LLM)
# ---------------------------------------------------------------------------

def eval_sample_dedup(sample: dict, max_days: int | None = None) -> dict:
    """Replay the stream through the pipeline's dedup stage and score it
    against dup_cluster_id ground truth."""
    archive = {}
    company = sample["company"]["name"] or sample["company"]["symbol"]
    seen_clusters = set()
    tp = fp = fn = tn = 0
    days = group_by_date(sample)
    if max_days:
        days = days[:max_days]
    for date, rows in days:
        texts = [item_text(r) for r in rows]
        # dedup_news maintains the archive itself (accepted items join the
        # comparison set immediately, enabling intra-batch dedup)
        new_items, duplicates = dedup_news(archive, company, texts)
        dup_texts = {t for t, _ in duplicates}
        for row, text in zip(rows, texts):
            # streaming ground truth: an item is a true duplicate iff its
            # cluster was already seen EARLIER in the stream — including
            # earlier rows of the same day's batch
            cluster_seen = row["dup_cluster_id"] in seen_clusters
            pipeline_says_dup = text in dup_texts
            if cluster_seen and pipeline_says_dup:
                tp += 1
            elif cluster_seen and not pipeline_says_dup:
                fn += 1
            elif not cluster_seen and pipeline_says_dup:
                fp += 1
            else:
                tn += 1
            seen_clusters.add(row["dup_cluster_id"])
    total = tp + fp + fn + tn
    return {
        "sample_id": sample["sample_id"],
        "type": sample["type"],
        "n_items": total,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
    }


# ---------------------------------------------------------------------------
# Full pipeline evaluation (LLM)
# ---------------------------------------------------------------------------

# errors that retrying will never fix — fail fast instead of burning the
# whole retry budget per day and draining the sample with error entries
PERMANENT_ERROR_PATTERNS = (
    "insufficient balance", "authentication", "invalid api key",
    "incorrect api key", "invalid_api_key", "permission denied", "401",
)


def _retry(fn, attempts: int = 4, wait: float = 15.0):
    """Retry an LLM call through transient network failures. A dropped
    connection otherwise hangs or kills a multi-hour run. Permanent errors
    (billing, auth) are raised immediately."""
    import time
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as e:
            msg = str(e).lower()
            if any(p in msg for p in PERMANENT_ERROR_PATTERNS):
                raise
            if attempt == attempts - 1:
                raise
            print(f"    retry {attempt + 1}/{attempts} after: {type(e).__name__}: {e}")
            time.sleep(wait * (attempt + 1))


def eval_sample_full(llm, workflow, long_term, sample: dict,
                     max_days: int | None = None) -> dict:
    """Replay one sample through the whole pipeline; return per-day verdicts
    plus dedup confusion counts."""
    company = sample["company"]["name"] or sample["company"]["symbol"]
    short_term = ShortTermMemory(max_size=12)
    archive = {}
    seen_clusters = set()
    dedup_counts = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    day_results = []

    days = group_by_date(sample)
    if max_days:
        days = days[:max_days]
    for date, rows in days:
        texts = [item_text(r) for r in rows]
        new_items, duplicates = dedup_news(archive, company, texts)
        dup_texts = {t for t, _ in duplicates}
        for row, text in zip(rows, texts):
            cluster_seen = row["dup_cluster_id"] in seen_clusters
            predicted_dup = text in dup_texts
            key = ("tp" if cluster_seen and predicted_dup
                   else "fn" if cluster_seen
                   else "fp" if predicted_dup else "tn")
            dedup_counts[key] += 1
            seen_clusters.add(row["dup_cluster_id"])

        entry = {"date": date, "n_crawled": len(rows), "n_new": len(new_items),
                 "n_duplicates": len(duplicates),
                 "band": derived_risk_level(sample, date),
                 "verdict": None}
        if new_items:
            news_batch = stage_news(short_term, company, new_items)
            profile, profile_id = _retry(lambda: recall_profile(long_term, company))
            profile_text = (json.dumps(profile, ensure_ascii=False, indent=2)
                            if profile else "None")
            try:
                outcome = _retry(lambda: assess(workflow, company, news_batch, profile_text))
            except Exception as e:
                print(f"    day {date} failed after retries: {e}")
                entry["error"] = str(e)
                day_results.append(entry)
                continue
            archive_news(long_term, archive, company, date, new_items)
            # record the verdict even on no-event days — the workflow already
            # ran (the calls are paid for) and the rubric returns low/0 for
            # uneventful news, which IS the per-day signal for negatives;
            # only profile curation + reflection are skipped (nothing changed)
            entry["verdict"] = outcome["verdict"]
            entry["events"] = outcome["events"]
            if outcome["events"]:
                new_profile = _retry(lambda: update_profile(
                    llm, company, date, profile, outcome["events"], outcome["verdict"]))
                reflection = _retry(lambda: reflect(llm, company, new_profile, outcome["verdict"]))
                if reflection:
                    new_profile["reflection"] = reflection
                save_profile(long_term, company, new_profile, profile_id)
        day_results.append(entry)

    verdict_days = [d for d in day_results if d["verdict"]]
    final = next((d["verdict"] for d in reversed(verdict_days)
                  if d["verdict"].get("risk_level") in LEVEL_ORDER), None)
    first_flag = next(
        (d for d in verdict_days
         if LEVEL_ORDER.get(str(d["verdict"].get("risk_level")), 0)
         >= LEVEL_ORDER["high"]),
        None)
    lead_days = None
    if first_flag and sample["type"] == "positive":
        event = datetime.strptime(sample["label"]["event_date"], "%Y-%m-%d")
        lead_days = (event - datetime.strptime(first_flag["date"], "%Y-%m-%d")).days
    # trajectory agreement: predicted level vs. derived band, per assessed day
    band_hits = band_total = 0
    for d in verdict_days:
        pred = str(d["verdict"].get("risk_level"))
        if pred in LEVEL_ORDER:
            band_total += 1
            band_hits += int(pred == d["band"])
    return {
        "sample_id": sample["sample_id"],
        "type": sample["type"],
        "split": sample.get("split"),
        "company": company,
        "event_date": sample["label"]["event_date"],
        "n_items": sum(d["n_crawled"] for d in day_results),
        "dedup": dedup_counts,
        "n_days": len(day_results),
        "n_verdict_days": len(verdict_days),
        "final_risk_level": final.get("risk_level") if final else None,
        "final_score": final.get("score") if final else None,
        "first_high_or_critical": first_flag["date"] if first_flag else None,
        "lead_days": lead_days,
        "band_accuracy": band_hits / band_total if band_total else None,
        "days": day_results,
    }


def summarize(results: list[dict]) -> dict:
    failed = [r for r in results if "error" in r and "dedup" not in r]
    results = [r for r in results if r not in failed]
    pos = [r for r in results if r["type"] == "positive"]
    neg = [r for r in results if r["type"] == "negative"]

    def frac(rows, pred):
        return (sum(1 for r in rows if pred(r)) / len(rows)) if rows else None

    dedup = defaultdict(int)
    for r in results:
        for k, v in r["dedup"].items():
            dedup[k] += v
    tp, fp, fn = dedup["tp"], dedup["fp"], dedup["fn"]
    return {
        "n_samples": len(results),
        "n_failed": len(failed),
        "failed_samples": [r["sample_id"] for r in failed],
        "positive": {
            "n": len(pos),
            "flagged_high_or_critical": frac(
                pos, lambda r: LEVEL_ORDER.get(str(r["final_risk_level"]), 0)
                >= LEVEL_ORDER["high"]),
            "mean_lead_days": (sum(r["lead_days"] for r in pos if r["lead_days"] is not None)
                               / max(1, sum(1 for r in pos if r["lead_days"] is not None)))
                              if any(r["lead_days"] is not None for r in pos) else None,
        },
        "negative": {
            "n": len(neg),
            "false_alarm_high_or_critical": frac(
                neg, lambda r: LEVEL_ORDER.get(str(r["final_risk_level"]), 0)
                >= LEVEL_ORDER["high"]),
        },
        "dedup": {
            **dict(dedup),
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
        },
        "band_accuracy_mean": (
            sum(r["band_accuracy"] for r in results if r["band_accuracy"] is not None)
            / max(1, sum(1 for r in results if r["band_accuracy"] is not None))),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", default="projects/credit_risk/dataset/v0.2/dev.jsonl")
    parser.add_argument("--n-samples", type=int, default=None)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--indices", default=None,
                        help="comma-separated sample indices (overrides --offset/--n-samples); "
                             "combine with EAX_STORE_DIR to run parallel slices")
    parser.add_argument("--max-days", type=int, default=None,
                        help="cap the number of distinct news days per sample (smoke tests)")
    parser.add_argument("--dedup-only", action="store_true",
                        help="evaluate only the news dedup stage (no LLM needed)")
    parser.add_argument("--threshold", type=float, default=None,
                        help="override the demo's NEWS_DUP_THRESHOLD (0.88)")
    parser.add_argument("--tag", default=None, help="output file tag")
    args = parser.parse_args()

    if args.threshold is not None:
        import credit_risk_demo
        credit_risk_demo.NEWS_DUP_THRESHOLD = args.threshold

    # parallel slices must not share the FAISS/SQLite store
    if os.environ.get("EAX_STORE_DIR"):
        import credit_risk_demo
        credit_risk_demo.STORE_DIR = os.environ["EAX_STORE_DIR"]

    with open(args.input, encoding="utf-8") as f:
        samples = [json.loads(line) for line in f]
    if args.indices:
        idxs = [int(x) for x in args.indices.split(",")]
        samples = [samples[i] for i in idxs]
    else:
        samples = samples[args.offset:args.offset + args.n_samples if args.n_samples else None]
    print(f"{len(samples)} samples from {args.input}")

    os.makedirs(OUT_DIR, exist_ok=True)
    tag = args.tag or datetime.now().strftime("%Y%m%d_%H%M%S")

    if args.dedup_only:
        results = [eval_sample_dedup(s, args.max_days) for s in samples]
        tp = sum(r["tp"] for r in results)
        fp = sum(r["fp"] for r in results)
        fn = sum(r["fn"] for r in results)
        tn = sum(r["tn"] for r in results)
        summary = {
            "mode": "dedup-only", "input": args.input, "n_samples": len(results),
            "items": tp + fp + fn + tn,
            "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
        }
    else:
        from dotenv import load_dotenv
        load_dotenv()
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise SystemExit("DEEPSEEK_API_KEY not found — set it or use --dedup-only.")
        # pre-flight: fail fast on billing/auth problems instead of after
        # hours of retries (DeepSeek: GET /user/balance)
        import urllib.request
        try:
            req = urllib.request.Request(
                "https://api.deepseek.com/user/balance",
                headers={"Authorization": f"Bearer {api_key}"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                info = json.loads(resp.read())
            balances = info.get("balance_infos", [])
            available = info.get("is_available", True) and any(
                float(b.get("total_balance", 0)) > 0 for b in balances)
            print(f"DeepSeek balance: {balances}")
            if not available:
                raise SystemExit("DeepSeek account has no usable balance — top up first.")
        except SystemExit:
            raise
        except Exception as e:
            print(f"WARNING: balance check failed ({e}); continuing anyway")
        from llm import get_evoagentx_llm
        llm = get_evoagentx_llm()  # default provider from llm/providers.json
        skills = SkillManager(skill_paths=SKILLS_DIR)
        graph = build_graph(skills.get_skill("credit_risk_taxonomy").content,
                            skills.get_skill("risk_scoring_rubric").content)
        agent_manager = AgentManager()
        agent_manager.add_agents_from_workflow(graph, llm.config)
        workflow = WorkFlow(graph=graph, agent_manager=agent_manager, llm=llm)
        long_term = build_memory()
        seed_case_library(long_term)

        results = []
        # per-sample checkpoint: a killed run keeps completed samples and
        # re-running with the same --tag skips them (resume)
        ckpt_path = os.path.join(OUT_DIR, f"ckpt_{tag}.jsonl")
        done = {}
        if os.path.exists(ckpt_path):
            with open(ckpt_path, encoding="utf-8") as f:
                for line in f:
                    r = json.loads(line)
                    done[r["sample_id"]] = r
            print(f"checkpoint: {len(done)} samples already done, resuming")
        for i, sample in enumerate(samples):
            if sample["sample_id"] in done:
                continue
            print(f"[{i + 1}/{len(samples)}] {sample['sample_id']} "
                  f"({sample['company']['name'] or sample['company']['symbol']})")
            try:
                r = eval_sample_full(llm, workflow, long_term, sample,
                                     args.max_days)
            except Exception as e:
                print(f"  SAMPLE FAILED: {sample['sample_id']}: {type(e).__name__}: {e}")
                r = {"sample_id": sample["sample_id"],
                     "type": sample["type"], "error": str(e)}
            with open(ckpt_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
            done[r["sample_id"]] = r
        results = [done[s["sample_id"]] for s in samples if s["sample_id"] in done]
        summary = {"mode": "full", "input": args.input, **summarize(results)}
        long_term.save()

    out_path = os.path.join(OUT_DIR, f"eval_{tag}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "results": results}, f,
                  ensure_ascii=False, indent=2, default=str)
    print(json.dumps(summary, indent=2))
    print(f"saved -> {out_path}")


if __name__ == "__main__":
    main()
