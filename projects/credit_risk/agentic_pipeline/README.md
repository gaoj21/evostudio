# Agentic Credit-Risk Alert Pipeline

The next-generation credit-risk workflow: instead of replaying a known
company's news window (the `credit_risk_demo.py` generation), raw source
items arrive unattributed, and the pipeline itself works out WHO the news is
about, WHETHER we cover that obligor, and WHETHER it deserves an alert.

```
            ┌─────────────────────────── step 1: SOURCING ──────────────────────────┐
 raw news ──┤ entity extraction (LLM): who is the SUBJECT of each headline          ├─► candidates
 8-K filing ┤ registrant CIK from EDGAR metadata (no LLM needed)                    ├─► (name|CIK)
            └────────────────────────────────────────────────────────────────────────┘
                                          │
                    step 2: GROUNDING — ObligorRegistry.match (CIK direct,
                    normalized name, alias, token-fuzzy). No match ⇒ drop & log.
                                          ▼
            ┌──────────────── step 3: REASONING LOOP (per obligor × day) ───────────┐
            │  DETECT      main subject? topic (8-class taxonomy), severity, conf.  │
            │  INVESTIGATE memory retrieval: risk profile + past alerts + cases     │
            │  REFLECT     revalidate topic/severity against profile & history      │
            │  DECIDE      rubric-governed verdict: alert | suppress (+level/score) │
            └────────────────────────────────────────────────────────────────────────┘
                                          ▼
   step 4: ACTION — alert → Alert record; suppress → reasoned log entry
                                          ▼
   step 5: MEMORY — alert repository (LTM, per obligor) · risk profile
           (risk_focus = industry + themes, trajectory) · news archive (dedup)
                                          ▼
   step 7: EVALUATION — weight of evidence (source × severity × confidence
           × corroboration × trajectory consistency); gray-zone WoE routes
           to human feedback (AutoApprover default, HITLManager adapter)
                                          ▼
   step 8: SELF-EVOLVING — agent prompts are module constants in agents.py;
           MiproOptimizer-ready (see ../optimize_mipro.py); export_traces()
           dumps agent IO for offline scoring; topology variants are cheap
           (the loop is a plain function chain).
```

## Files

| module | step | contents |
|---|---|---|
| `sourcing.py` | 1 | `RawItem`, LLM entity extraction (news), CIK read-off (8-K) |
| `obligors.py` | 2 | `ObligorRegistry`: CIK / exact-name / alias / fuzzy matching; loads from `contemporary/candidates.csv` |
| `agents.py` | 3 | `detect` / `investigate` / `reflect` / `decide` (investigate is pure retrieval — no LLM) |
| `pipeline.py` | 3–5 | `AlertPipeline.ingest(items)` orchestration; `AlertRepository` in LTM; profile updates reuse the demo's curator |
| `evaluate.py` | 7 | `weight_of_evidence`, `AutoApprover`, `make_hitl_hook` (evoagentx.hitl adapter) |
| `evolve.py` | 8 | `export_traces`; optimization entry is `../optimize_mipro.py` |
| `run_demo.py` | — | end-to-end smoke test on a synthetic feed |

Reused from `credit_risk_demo.py` (injected via `memory_fns`, not copied):
news dedup, news archive, profile recall/save/update, case library, LTM
builder. Skills `credit_risk_taxonomy` / `risk_scoring_rubric` feed the
Detect and Decide prompts.

## What the smoke test verifies

- unknown company → dropped at grounding
- roundup wire mentioning a covered obligor → entity extraction returns no
  subject → dropped (the v0.3 roundup-noise defense, by construction)
- reworded same-day duplicate → killed by dedup, one alert
- 8-K with CIK → grounded by CIK, escalates vs. the previous day's alert
- gray-zone alert → routed to the human-feedback hook

Run:

```bash
.venv/bin/python projects/credit_risk/agentic_pipeline/run_demo.py
```

## Next

- `run_contemporary_eval.py`: replay `projects/credit_risk/dataset/contemporary/`
  samples (GDELT news + EDGAR 8-K filings) through this pipeline; labels give
  detection / lead-time metrics, `gold_risk_type`-style annotation for topic
  accuracy comes after re-labeling the contemporary news.
- Wire `make_hitl_hook` into the HITL GUI for the demo.
