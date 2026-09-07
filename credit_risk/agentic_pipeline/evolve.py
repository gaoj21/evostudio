"""Step 8 — Self-evolving layer: hooks into prompt/skill optimization.

The four agents' prompts are module-level constants in `agents.py`
(DETECT_PROMPT / REFLECT_PROMPT / DECIDE_PROMPT, plus the entity-extraction
prompt in `sourcing.py`). That layout is deliberate: the pipeline can be
wrapped as a MiproRegistry program and handed to `MiproOptimizer`, exactly
like the 3-node ancestor in `examples/credit_risk/optimize_mipro.py`
(which optimized the extract/analysis/verdict instructions of the previous
pipeline generation on the v0.2 dataset).

`export_traces` turns a run's PipelineResult into optimizer-ready records:
one JSON object per (obligor, day) group with the inputs and outputs of each
agent, so candidate prompts can be scored offline without re-running the
loop.

Topology search (e.g. AFlow over loop variants — drop Reflect, reorder
Investigate/Detect) is future work; the loop is a plain function chain in
`pipeline.AlertPipeline._process_group`, so variants are cheap to express.
"""

import json
import os


def export_traces(result, out_path: str):
    """Dump per-group agent IO from a PipelineResult as JSONL records."""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    n = 0
    with open(out_path, "w", encoding="utf-8") as f:
        for alert in result.alerts:
            f.write(json.dumps({
                "obligor_id": alert["obligor_id"], "date": alert["date"],
                "detect": alert["detection"], "reflect": alert["reflection"],
                "decide": alert["decision"],
            }, ensure_ascii=False) + "\n")
            n += 1
        for sup in result.suppressed:
            f.write(json.dumps({
                "obligor": sup["obligor"], "date": sup["date"],
                "detect": sup.get("detection"), "decide": "suppress",
            }, ensure_ascii=False) + "\n")
            n += 1
    return n
