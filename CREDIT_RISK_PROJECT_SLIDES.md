# Credit Risk Monitoring — Application Overview for Slides

Updated: September 14, 2026. Workflow details reflect the saved Credit Risk Monitoring graph; dataset statistics reflect the final eligible-trajectories release. No new experiment was run to prepare this document.

## Slide 1 — Project Objective

**Can an agent recognize worsening company risk before a verified event by accumulating public evidence over time?**

The project applies EvoStudio to company-level credit risk monitoring. News and filings are processed as a time trajectory, with previous assessments available to later decisions.

- Identify which monitored company an item concerns.
- Detect credit-relevant developments in the supplied evidence.
- Interpret new information alongside earlier observations.
- Decide whether to alert or suppress, with a risk assessment and rationale.
- Evaluate event detection and warning timing against independently verified outcomes.

The broader workflow can reason about several credit-risk themes. The final dataset evaluates a narrower target: verified bankruptcy events.

## Slide 2 — Inputs and Entity Grounding

**Public evidence must be connected to the correct monitored obligor.**

- News inputs contain published company-related items.
- Filing inputs contain public disclosures and available filing metadata.
- An obligor registry provides company identity matching through tools.
- Observation dates define which evidence is available at each step.
- The platform also supports a separate shared reference input for a user-supplied obligor list; using it requires the corresponding graph mappings and matching logic.

The saved workflow separates news subject extraction from filing metadata extraction, then grounds candidates using `ObligorMatchToolkit`.

News sourcing is instructed to identify main-subject companies rather than incidental mentions. Grounding prioritizes CIK matches before company-name matches. Downstream detection is instructed to suppress unmatched items.

Speaker note: The current dataset has company-specific trajectories. Extracting several company names from a news batch is not the same as automatically creating a separate trajectory for every company mentioned.

## Slide 3 — Current Workflow

```text
                         ┌─ News subject extraction ─┐
Dataset / input feed ────┤                           ├─ Grounding
                         └─ Filing metadata extraction┘      │
                                                             ▼
                     Detect → Investigate → Reflect → Decide
                                  ▲                      │
                                  └── Dated memory ──────┘
```

The diagram summarizes the main sequence. The actual graph also maps source fields and intermediate outputs directly to later nodes where needed.

| Stage | Role |
|---|---|
| Source news | Extract the main-subject companies from supplied news. |
| Source filings | Extract available registrant CIK and filing item numbers. |
| Grounding | Match candidates to monitored obligors using registered tools. |
| Detect | Identify the relevant risk topic, severity, confidence, and evidence summary. |
| Investigate | Assemble current context using detection and prior company memory. |
| Reflect | Reassess severity and confidence against that context. |
| Decide | Produce an alert/suppress action, risk level, score, and rationale. |

The current saved graph contains one source node and seven processing nodes. It uses the standard workflow execution path. The repository also contains a separate Python `agentic_pipeline` implementation; its internal behavior should not be presented as identical to this canvas graph.

## Slide 4 — Memory and Temporal Reasoning

**Memory connects a new observation to the company's prior risk trajectory.**

The saved workflow enables dated table memory on **Investigate** and **Decide**:

- Records are associated with the company and observation date.
- Recall selects the same company's earlier records.
- Investigate reads previous context and final decisions.
- Decide reads previous decisions.
- Successful node outputs are persisted according to the configured policy.

The prompts instruct the agents to:

- Carry forward confirmed risks that remain unresolved.
- Distinguish a repeated report from a genuinely new development.
- Preserve the existing risk view when a weak new item adds no resolution.
- Adjust assessments when supported by new evidence.
- Avoid inventing history or treating an earlier context summary as proof that an alert was issued.

These are implemented policies and intended behaviors, not measured evidence that memory improves performance. The platform offers shared Mem0 and Deep Agents integrations, but the saved Credit Risk graph currently uses table memory and the standard workflow runtime.

## Slide 5 — Decisions and Inspectability

The final node produces structured decision fields:

| Field | Meaning |
|---|---|
| `action` | Alert or suppress the current development. |
| `risk_level` | Low, medium, high, or critical. |
| `score` | Rubric-based assessment on a 0–100 scale. |
| `rationale` | Explanation based on the supplied evidence and context. |

The workflow applies domain instructions for event classification and risk scoring. Its prompts currently contain these rules directly; the saved nodes do not attach separate Library skills.

Results retain inputs and intermediate outputs so reviewers can inspect how entity matching, detection, context, and reflection contributed to a decision. Result Chat can help summarize or compute over those saved outputs.

**The score is a rubric-based agent output, not a calibrated probability of default.** Suppressing a repeated alert also does not necessarily mean the company's risk level has returned to low.

## Slide 6 — Evaluation Dataset

The final release contains only trajectories eligible for the current pre-event bankruptcy evaluation.

| Metric | Total | Dev | Test |
|---|---:|---:|---:|
| Companies | 53 | 30 | 23 |
| Trajectories | 53 | 30 | 23 |
| Observations | 648 | 397 | 251 |

- **1,006 unique evidence documents:** 376 cached news bodies, 305 headline-only records, 324 cached filing bodies, and one identified primary-source paraphrase.
- **6 broad industry groups and 36 SIC codes.**
- **Target events:** 44 Chapter 11, 8 Chapter 7, and 1 Canadian bankruptcy assignment.
- **Selection:** 53 retained from 107 candidate trajectories, using verified event type, registrant scope, and event-after-window criteria.
- **Split:** Original company assignments are preserved; the final 30/23 split is approximately 57:43.

Evidence is organized by publication date. Event outcomes are stored separately and excluded from workflow inputs. The 648 observations are time steps within 53 evaluation cases, not 648 independent companies or trajectories.

## Slide 7 — Evaluation and Prompt Improvement

Two different evaluation settings must be distinguished:

| Setting | What it can assess |
|---|---|
| Sequential trajectory replay | Whether and when an alert appears as observations and memory accumulate. |
| End-of-window snapshot evaluation | The model's assessment from the selected window snapshot; not an early-warning timing experiment. |

Suggested sequential evaluation measures:

- **Event detection rate:** Fraction of target trajectories with a valid pre-event warning.
- **Lead time:** Days from the first valid warning to the verified event.
- **Evidence quality:** Relevance, temporal availability, and support for the warning.
- **Memory comparison:** Matched runs with and without memory, using the same model, inputs, and decision rules.

The platform can evaluate saved results without rerunning the workflow and propose prompt revisions from those traces. It also provides an execution-based MIPRO optimization path. A proposed prompt change requires new evaluation before claiming improvement.

The current Credit Risk Evolve dataset path uses end-of-window snapshots. It must not be presented as a sequential memory evaluation.

## Slide 8 — Current Scope and Limitations

**What is implemented:** A configurable monitoring workflow, entity-matching tools, dated company memory, inspectable decisions, versioned evaluation data, and evaluation/prompt-refinement interfaces.

**What remains an experimental question:** Whether these components improve detection, lead time, reliability, or cost under a controlled comparison.

- The final dataset is positive-only: no verified negative cases are included.
- Overall binary classification accuracy and false-positive rate cannot be measured from this release.
- Sparse evidence, headline-only records, and industry imbalance limit generalization.
- Event verification and date filtering do not prove the absence of all evidence noise or model prior-knowledge leakage.
- Sequential experiments need chronological execution, controlled memory initialization, and consistent warning criteria.
- If Test data have influenced prompt development, that use must be disclosed.

No numerical detection result or before/after improvement is claimed in this presentation. Those values should come from a specified saved run, dataset version, and evaluation protocol.

## Suggested Demo Sequence

1. Show the two evidence branches and explain obligor grounding.
2. Open Investigate and Decide memory settings to show company/date scoping.
3. Select one trajectory and step through its observations chronologically.
4. Compare the current detection with retrieved context and the final decision.
5. Inspect the saved input and intermediate outputs for one warning.
6. Open evaluation for the completed run and explain coverage and missed cases.
7. Show a proposed prompt revision while keeping measured results separate from proposed changes.

## Short Presentation Script

> Credit Risk Monitoring is a domain application built on EvoStudio. It combines news and public filings, grounds them to monitored companies, and processes them through detection, investigation, reflection, and decision stages. Dated company memory lets later observations reuse earlier assessments while distinguishing repeated reports from new evidence. Our final evaluation dataset contains 53 company trajectories, 648 observations, and 1,006 unique documents. The research objective is to determine whether this sequential process improves event detection and warning lead time. The current release contains only positive bankruptcy cases, so it does not measure overall classification accuracy or false-positive rate, and prompt improvements still require controlled validation.

## Repository References

- [Saved Credit Risk Monitoring workflow](backend/data/graphs/credit-risk-monitoring.json)
- [Final dataset](projects/credit_risk/dataset/expansion/releases/2026-09-14-eligible-trajectories-v1/)
- [Dataset slide content](CREDIT_RISK_DATASET_SLIDES.md)
- [Detailed current dataset profile](projects/credit_risk/docs/dataset/DATASET_CURRENT_PROFILE.md)
- [Separate Python pipeline implementation](projects/credit_risk/agentic_pipeline/README.md)
- [Platform overview](EVOSTUDIO_OVERVIEW_SLIDES.md)
