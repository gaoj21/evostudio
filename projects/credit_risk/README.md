# Credit Risk Early-Warning — Application Layer

This directory is a self-contained sub-project layered on top of the
**EvoAgentX framework** (the bottom layer, unchanged in `evoagentx/`).

```
┌─────────────────────────────────────────────────────────┐
│  projects/credit_risk/  — application layer (this sub-project)    │
│                                                          │
│  agentic_pipeline/   8-stage agentic alert workflow      │
│  credit_risk_demo.py baseline 3-node pipeline + memory   │
│  run_dataset_eval.py dataset evaluation runner           │
│  optimize_mipro.py   MIPRO prompt optimization           │
│  skills/             credit-risk skill packs             │
│  dataset/            dataset builders + data + docs      │
│  output/             eval / optimization artifacts       │
├─────────────────────────────────────────────────────────┤
│  evoagentx/  — framework layer (agents, memory, HITL,    │
│  optimizers, models, skills runtime). Not modified here. │
└─────────────────────────────────────────────────────────┘
```

Dependency direction is strictly top-down: everything here imports from
`evoagentx`, never the reverse. Run all commands from the **repository
root** so that both layers are importable.

## Layout

| Path | Contents |
|---|---|
| `agentic_pipeline/` | Sourcing (news entity extraction / 8-K CIK) → obligor grounding → 4-agent loop (detect / investigate / reflect / decide) → action → memory update → evaluation (WoE + human feedback) → self-evolving layer. See `agentic_pipeline/README.md`. |
| `docs/AGENT_SKILLS.md` | Per-agent map of which skill / prompt each agent uses and where to edit it — the entry point for tuning and optimization. |
| `docs/HANDOFF_AGENT.md` | 交接文档:agentic pipeline 最终版(架构、模块、数据结构、改动入口)。 |
| `docs/HANDOFF_DATASET.md` | 交接文档:数据集最终版(contemporary + v0.4 构成、schema、构建链、GDELT 运维教训)。 |
| `credit_risk_demo.py` | Original sequential pipeline (event extraction → analysis → verdict) plus the shared long-term-memory helpers injected into the agentic pipeline. |
| `run_dataset_eval.py` | Replays a dataset split through the baseline pipeline; metrics (detection, lead time, false alarms, dedup). |
| `optimize_mipro.py` | MIPRO prompt optimization over the baseline pipeline (concluded: seed instruction was already optimal). |
| `skills/` | `credit_risk_taxonomy`, `risk_scoring_rubric`. |
| `dataset/` | `builders/` contains build scripts; raw sources, versioned splits, expansion and review data remain here. Design and statistics documents live in `docs/dataset/`. |
| `output/` | Memory stores, eval results, MIPRO artifacts. |

## Quick start

```bash
# Agentic pipeline smoke demo (LLM, ~$0.01)
.venv/bin/python projects/credit_risk/agentic_pipeline/run_demo.py

# Baseline evaluation on a dataset split
.venv/bin/python projects/credit_risk/run_dataset_eval.py \
    --input projects/credit_risk/dataset/v0.2/dev.jsonl
```

## Compatibility notes

- `data/credit_risk_dataset` is a symlink to `projects/credit_risk/dataset`, kept so
  long-running fetch jobs started before the reorganization keep writing to
  the right place. New work should reference `projects/credit_risk/dataset` directly.
- Dataset design docs still cite the pre-move paths in a few examples; they
  will be refreshed when the `contemporary/` dataset build lands.

## 目录与数据说明

- [项目目录层级](../../docs/project-layout.md)
- [当前数据集构建方法与统计](docs/dataset/DATASET_CONSTRUCTION_AND_PROFILE.md)
