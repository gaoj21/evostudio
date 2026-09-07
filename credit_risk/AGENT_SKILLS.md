# Agent ↔ Skill Map

Quick reference for which skill / prompt each agent uses, and where to edit
it. Two kinds of tunables exist:

- **Skills** — domain standards living in `skills/<name>/SKILL.md`. Injected
  into prompts verbatim. Treated as FIXED during prompt optimization (they
  are the credit-risk domain standard, not a tuning knob).
- **Prompts** — per-agent instruction strings, module-level constants in the
  source files. These are what the self-evolving layer / MIPRO optimizes.

## Available skills (`skills/`)

| Skill | File | Purpose |
|---|---|---|
| `credit_risk_taxonomy` | `skills/credit_risk_taxonomy/SKILL.md` | Closed list of credit-risk event types + severity definitions. Governs topic classification. |
| `risk_scoring_rubric` | `skills/risk_scoring_rubric/SKILL.md` | Risk levels, score bands, action mapping, output JSON schema. Governs final severity/action decisions. |

Loaded once via `SkillManager(skill_paths=SKILLS_DIR)` and injected as text:
- agentic pipeline: `agentic_pipeline/pipeline.py` → `build_pipeline()` (`taxonomy=`, `rubric=` kwargs)
- baseline demo: `credit_risk_demo.py` → `build_graph(taxonomy, rubric)`

## Agentic pipeline (`agentic_pipeline/`)

| Agent | File | Prompt constant | Skill injected | Notes |
|---|---|---|---|---|
| Sourcing — news entity extraction | `sourcing.py` | `ENTITY_EXTRACTION_PROMPT` | — (none) | Picks the MAIN subject company per headline. Candidate prompt for optimization. |
| Sourcing — 8-K CIK extraction | `sourcing.py` | `candidates_from_filings()` | — | Pure parsing, no LLM, no skill. |
| Grounding — obligor matching | `obligors.py` | — | — | Rule-based name/CIK matching against the internal obligor list. No LLM. |
| **Detect** | `agents.py` | `DETECT_PROMPT` | **`credit_risk_taxonomy`** | Confirms obligor is main subject, classifies alert topic (taxonomy event types), scores severity + confidence. |
| **Investigate** | `agents.py` | — | — | No LLM call: reads risk profile + past alerts from LTM (`memory_fns`). |
| **Reflect** | `agents.py` | `REFLECT_PROMPT` | — (none) | Revalidates topic/severity against profile & history. References taxonomy implicitly through the detection JSON only. |
| **Decide** | `agents.py` | `DECIDE_PROMPT` | **`risk_scoring_rubric`** | Issues alert / suppress per rubric score bands. |
| Action | `pipeline.py` | — | — | Deterministic: routes the decision. |
| Memory update | `pipeline.py` + `credit_risk_demo.py` helpers | `demo.reflect` prompt | — | Alert repository + risk profile writes via injected `memory_fns`. |
| Evaluation | `evaluate.py` | — | — | WoE formula (severity/confidence/source/corroboration/trajectory weights) + `AutoApprover` human routing. Weights are constants in `weight_of_evidence()`. |
| Self-evolving | `evolve.py` | — | — | Offline prompt-scoring hooks; reads the prompt constants above. |

## Baseline pipeline (`credit_risk_demo.py`, 3 nodes)

| Node | Prompt location | Skill injected |
|---|---|---|
| `extract_events` | `build_graph()` — node 1 `prompt` | **`credit_risk_taxonomy`** |
| `risk_analysis` | `build_graph()` — node 2 `prompt` | — (profile + cases only) |
| `risk_verdict` | `build_graph()` — node 3 `prompt` | **`risk_scoring_rubric`** |

## How to modify

- Change event types / severity definitions → edit
  `skills/credit_risk_taxonomy/SKILL.md`; both pipelines pick it up on next
  run (no code change).
- Change score bands / action mapping / verdict JSON schema → edit
  `skills/risk_scoring_rubric/SKILL.md`.
- Change an agent's instructions → edit the prompt constant listed above.
  `optimize_mipro.py` optimizes the baseline prompts (skills stay fixed);
  `agentic_pipeline/evolve.py` scores candidate prompts for the agentic
  pipeline offline.
