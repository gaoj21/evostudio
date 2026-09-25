"""End-to-end smoke demo of the agentic alert pipeline.

A tiny synthetic feed exercises every step without waiting for the
contemporary dataset build:

- an unknown company in the news (must be dropped at grounding)
- a roundup wire mentioning a covered obligor (Detect should reject:
  mentioned, not the main subject)
- a real deteriorating-credit sequence for NexaSteel Group (alert)
- an 8-K channel item carrying CIK metadata (grounding by CIK)
- a stable company with good news (suppress / carry-forward)

Run from the repository root (needs DEEPSEEK_API_KEY in .env):
    .venv/bin/python examples/projects/credit_risk/agentic_pipeline/run_demo.py
"""

import json
import os
import sys

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..", "..")))  # repo root (llm/, backend/)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..")))

from dotenv import load_dotenv  # noqa: E402

from backend.features.model_bridge import workflow_model  # noqa: E402
from agentic_pipeline import (AutoApprover, Obligor, ObligorRegistry,  # noqa: E402
                              RawItem, build_pipeline, weight_of_evidence)

STORE_DIR = os.path.join("projects", "credit_risk", "output",
                         "store_agentic_demo")

REGISTRY = ObligorRegistry([
    Obligor(obligor_id="OB-001", name="NexaSteel Group",
            cik="9000001", symbol="NXST", industry="steel"),
    Obligor(obligor_id="OB-002", name="BrightHarvest Foods",
            cik="9000002", symbol="BHF", industry="consumer staples"),
])

FEED = [
    # unknown obligor -> grounding drop
    RawItem(source="news", date="2025-03-10",
            title="Zephyr Logistics files for Chapter 11 protection",
            publisher="newswire"),
    # covered obligor mentioned, but not the subject -> detect reject
    RawItem(source="news", date="2025-03-10",
            title="NexaSteel Group and BrightHarvest Foods among materials "
                  "gainers; Acme Widgets among losers",
            publisher="roundup-wire"),
    # real negative signal
    RawItem(source="news", date="2025-03-11",
            title="NexaSteel Group misses interest payment on its 2027 "
                  "senior notes, enters 30-day grace period",
            publisher="reuters"),
    # duplicate of the same event, reworded -> dedup should kill
    RawItem(source="news", date="2025-03-11",
            title="NexaSteel Group fails to pay interest on 2027 senior "
                  "notes; 30-day grace period begins",
            publisher="benzinga"),
    # 8-K channel: CIK metadata present, no entity extraction needed
    RawItem(source="8k", date="2025-03-12",
            title="NexaSteel Group 8-K: Item 2.03 acceleration of a direct "
                  "financial obligation",
            text="Item 2.03. Creation of a Direct Financial Obligation... "
                 "the trustee delivered a notice of default and acceleration "
                 "with respect to the 2027 senior notes ...",
            cik="9000001", registrant_name="NexaSteel Group",
            items=["2.03"]),
    # stable company, positive news -> suppress or positive alert
    RawItem(source="news", date="2025-03-12",
            title="BrightHarvest Foods reports fifth consecutive year of "
                  "profit growth, secures oversubscribed credit line",
            publisher="newswire"),
]


def main():
    load_dotenv()
    llm = workflow_model()  # default provider from llm/providers.json

    pipeline = build_pipeline(llm, REGISTRY, store_dir=STORE_DIR,
                              fresh=True,
                              woe_evaluator=weight_of_evidence,
                              human_feedback=AutoApprover())
    result = pipeline.ingest(FEED)

    print("\n===== ALERTS =====")
    for a in result.alerts:
        print(f"[{a['date']}] {a['obligor']} via {a['source']}")
        print(f"  topic={a['detection'].get('topic')} "
              f"severity={a['detection'].get('severity')} "
              f"conf={a['detection'].get('confidence')}")
        print(f"  decision: {a['decision'].get('risk_level')}/"
              f"{a['decision'].get('score')} — {a['decision'].get('rationale')}")
        print(f"  WoE={a.get('weight_of_evidence')}  "
              f"feedback={a.get('human_feedback')}")
    print("\n===== SUPPRESSED =====")
    for s in result.suppressed:
        print(f"[{s['date']}] {s['obligor']}: {s.get('reason')}")
    print("\n===== DROPPED AT GROUNDING =====")
    for d in result.dropped_unmatched:
        print(f"  {d['title']}  (candidate: {d['candidate']})")
    print("\nlog:", *result.log, sep="\n  ")

    profile, _ = pipeline.mf["recall_profile"](pipeline.ltm,
                                               "NexaSteel Group")
    print("\n===== NexaSteel profile (memory) =====")
    print(json.dumps(profile, ensure_ascii=False, indent=2)[:800])


if __name__ == "__main__":
    main()
