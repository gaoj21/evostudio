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

## In Studio

Studio knows nothing about credit risk. This project reaches it only through
`studio_plugin.py`, loaded by `backend/features/plugins.py` (contract:
[`projects/README.md`](../README.md)). The code behind it lives in `studio/`:

| File | Provides |
|---|---|
| `studio_plugin.py` | `NAME = "Credit risk"`; `presets()`, `templates()`, `toolkits()`, `source_types()` |
| `studio/feed.py` | the `credit_risk` Input's records and `info` (legacy `dataset/contemporary/samples.jsonl`) |
| `studio/releases.py` | versioned releases under `dataset/expansion/releases/<id>/` |
| `studio/presets.py` | the seven `CR …` preset nodes, the template and its review rule |
| `studio/obligor_tool.py` | `ObligorMatchToolkit` |

**Input type `credit_risk`** ("Credit Risk Feed"). Config: `dataset`
(`contemporary` when the legacy file exists, plus every release id), `split`
(`""` for all, `dev`, `test`), `n` (trajectories; `0` = the entire split),
`seed` (42), `step` (`none` / `monthly` / `weekly` / `daily`, default
`monthly`). With a step, one company window becomes a series of dated records,
each seeing only the evidence up to its `as_of`. Outputs: `sample_id`,
`company`, `symbol`, `cik`, `window_start`, `window_end`, `as_of`,
`news_batch`, `filing_batch`, `sample_json`. The type declares
`sequence: {"group": "sample_id", "order": "as_of"}`, so a batch or canvas
Evolve runs each company's records in date order and blocks the rest of a
company after a failed step, and the batch preview reports companies and date
range. `watch_key: "sample_id"`. `GET /api/sources/credit_risk/info?dataset=<id>`
returns the available versions, splits and fields.

**Presets** (palette group "Credit risk"): `cr_source_news`, `cr_source_8k` →
`cr_grounding` (uses `ObligorMatchToolkit`) → `cr_detect` → `cr_investigate` →
`cr_reflect` → `cr_decide`. `cr_investigate` and `cr_decide` default to
`use_long_term_memory: true`. Skill texts from `skills/` are inlined into the
prompts.

**Template** `credit-risk-monitoring` ("Credit Risk Monitoring"): a
`credit_risk` Input feeding the seven nodes, with this review rule on the
graph:

```json
{"node": "decide", "score_field": "score", "range": [35, 65],
 "when": {"field": "action", "equals": "alert"},
 "show": ["action", "risk_level", "score", "rationale"],
 "approve_label": "alert", "reject_label": "suppress"}
```

An `alert` from `decide` with a score of 35–65 (or any output with
`review_required: true`) goes to Studio's Review panel; approving makes the
final action `alert`, rejecting `suppress`. A batch's `review_zone` replaces
the range.

**`ObligorMatchToolkit`**: `match_company_name` and `match_cik` against the
internal obligor list (`dataset/contemporary/candidates.csv`, loaded lazily
and cached per process).

**Evaluator `evaluators/monitoring_report.py`**: ordinary evaluator code, not
part of the platform. Upload it to an Evaluator node with timing "after the
entire batch". `build_evaluator(decision_field="decision",
trajectory_field="sample_id", date_field="as_of", label_key="case_id",
reasoning_nodes="detect,investigate,reflect,decide")` reports per company
(caught / missed, lead days, false alarms on verified negatives) and per step
(level against the countdown band, reasoning that names a future date).
Metrics: `detection_rate`, `false_alarm_rate`, `mean_lead_days`,
`countdown_near_rate`, `foresight_steps`, `failed_steps`; the usual Evolve
objective is `detection_rate` (maximize). Truth comes from the Evaluator's
separate labels (`evaluators/labels.py` builds them for a release) or, for the
contemporary dataset, from each record's `sample_json`. See
`evaluators/README.md`.

## Compatibility notes

- `data/credit_risk_dataset` is a symlink to `projects/credit_risk/dataset`, kept so
  long-running fetch jobs started before the reorganization keep writing to
  the right place. New work should reference `projects/credit_risk/dataset` directly.
- Dataset design docs still cite the pre-move paths in a few examples; they
  will be refreshed when the `contemporary/` dataset build lands.

## 目录与数据说明

- [项目目录层级](../../docs/project-layout.md)
- [当前数据集构建方法与统计](docs/dataset/DATASET_CONSTRUCTION_AND_PROFILE.md)
