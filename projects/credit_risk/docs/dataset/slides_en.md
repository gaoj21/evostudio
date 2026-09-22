---
marp: true
theme: default
paginate: true
---

# A Credit-Risk News Dataset for Agentic Evaluation

**204 company windows · 17,277 headlines · 2007–2020**

Day-by-day news streams ending right before a known outcome —
built to evaluate memory, denoising and early warning, not one-shot classification.

`data/credit_risk_dataset/`

---

## Why this dataset

Agentic credit-risk monitoring is **streaming + stateful**:

- news arrives day by day; risk state must evolve with it
- memory must dedup wire reprints and ignore noise
- value = warning **days to months early**, not a label after the fact

Existing benchmarks are one-shot text classification.
We needed a dataset that replays reality.

---

## Task definition

```
Input:  company + date-ordered headline stream (0..N per day)
Output: risk verdict per day {risk_level, score, trend, evidence}
Eval:   in-window verdicts  vs.  known outcome after the window
```

Design decisions:

- **180-day windows**, ending the day BEFORE the event — no lookahead
- **Headlines only** — weak signal on purpose; aggregation is the test
- **Derived day labels**: ≤30d → critical · 31–90d → high · 91–180d → medium · negative → low

---

## Sources & labels

| Piece | Source |
|---|---|
| Bankruptcy events | SEC EDGAR full-text search, 8-K `Item 1.03`, 2004–2023 → 1,806 candidates |
| Event verification | each candidate's **8-K body** fetched & checked (real item heading + bankruptcy keywords) — kills FTS false positives |
| News | FNSPID `All_external.csv` (Benzinga et al., 5.7GB, 1999–2023) |
| Negatives | alive (symbol, year) pairs, comparable density, 2:1 sampling, ETF/fund blocklist |

Lehman · WaMu · GM · Kodak · Sears · Hertz · PG&E · MF Global · …

---

## The hard part: company ↔ news linking

Delisted companies have no ticker in EDGAR → **name matching only**.
Quality gate — a candidate needs evidence:

- **R1**: one symbol covers ≥30% of matched rows
- **R2**: multi-token name is a contiguous phrase in ≥50% of titles
- **R3**: distinctive single-token key (≥7 chars, non-common) in ≥60%

plus row-level cleanup and a manual review log (`candidates_review.csv`).

---

## Schema (one sample)

```json
{
  "sample_id": "pos_1401106_2011-11-03",
  "type": "positive",
  "company": {"name": "MF Global Holdings Ltd.", "cik": "1401106"},
  "window": {"start": "2011-05-07", "end": "2011-11-02"},
  "label":   {"event": "bankruptcy", "event_date": "2011-11-03"},
  "split": "train",
  "news_quality": {"rows": 290, "noise_share": 0.014, "flags": []},
  "news": [{
    "date": "2011-10-27",
    "title": "Fitch downgrades MF Global; cites risk-taking",
    "dup_cluster_id": "c187",          // v0.2
    "noise_kind": null,                // v0.3
    "gold_risk_type": "rating_downgrade" // v0.4
  }]
}
```

---

## Versioning: additive annotation only

| | adds | why |
|---|---|---|
| **v0.1** | 204 samples (92 pos / 112 neg) | — |
| **v0.2** | `dup_cluster_id` per row · official train/dev/test | dedup is a tested capability; optimization needs a fixed split |
| **v0.3** | `noise_kind` per row · `news_quality` + flags per sample | 2 missed detections turned out to be **data noise**, not model errors |
| **v0.4** | `gold_risk_type` per row (8-class taxonomy, LLM silver) | risk classification needs ground truth |

v0.4 result: **4,658 rows labeled (27.0%)** — positive_development 1,334 ·
debt_default 913 · liquidity_stress 909 · lawsuit_regulatory 635 ·
earnings_warning 394 · operational_shock 206 · rating_downgrade 150 ·
management_turmoil 117 (full run: 1.9 h, $4.6)

Same samples, same rows, same order — **asserted at build time**.
Old baselines stay comparable forever.

---

## v0.2 — dedup ground truth

Union-find per sample: exact normalized title (any dates) or
token-Jaccard ≥ 0.90 within ±3 days; wire prefixes stripped.

- 17,277 rows → **14,584 clusters**; 2,693 duplicate rows (**15.6%**)
- duplicates **kept deliberately** — that is the real distribution
- split: per-class chronological 70/15/15 → **train 142 / dev 31 / test 31**, no company crosses splits

---

## v0.3 — noise audit

| noise_kind | meaning | rows |
|---|---|---|
| `roundup` | daily gainers/losers wires | 729 |
| `foreign_subject` | headline is about another company | 259 |
| `generic_ctx` | generic-phrase name match | 50 |

Total noise **6.0%** of rows; dataset-wide median noise share **2.8%** —
noise is highly concentrated.

Sample flags: `noisy` (>50%) · `thin_centric` (<15 real rows) · `generic_name`
→ **5 samples flagged** (99% / 82% / 72% / 69% / 57% noise)

---

## Example — MF Global (positive, train)

Label: bankruptcy filed 2011-11-03 · window ends 11-02 · 290 rows, 1.4% noise

```
05-19  MF Global posts loss as costs hurt profit        (earnings_warning)
09-26  MF Global fined for market manipulation          (lawsuit_regulatory)
10-16  Regulator directed MF Global to boost capital    (liquidity_stress)
10-26  S&P may cut MF Global rating to junk             (rating_downgrade)
10-27  Clients move money away as troubles grow         (liquidity_stress)
10-28  MF Global aims for sale by Monday                (liquidity_stress)
10-31  MF Global files for Chapter 11                   (debt_default)
10-31  LME suspends MF Global from trading              (operational_shock)
11-02  CME: customer shortfall of $633 mln              (liquidity_stress)
```

A textbook escalation arc — liquidity stress ~90d out, downgrades ~31d,
collapse coverage in the final days. The trajectory bands match.

---

## Example — Micron (negative, train)

No event · window 2018-06-23 ~ 12-20 · 201 rows · 7% noise

```
06-26  UBS Upgrades Micron Technology to Neutral
06-26  UBS Upgrades Micron to Neutral; Raises PT to $60   ← same dup_cluster_id
06-26  DRAMeXchange: Server DRAM supply to improve in Q3
```

Plus distractors: "Bulls & Bears of the Week",
"26 Stocks Moving in Tuesday's Pre-Market Session" (recurring title =
cross-date dup cluster). Negatives exercise dedup & noise resistance too.

---

## Example — a flagged sample (test)

**Global Healthcare REIT**: the name contains a generic industry phrase.
72 matched rows, only 1 truly about the company:

```
Codexis (CDXS) Presents At UBS Global Healthcare Conference      (generic_ctx)
Community Health Systems (CYH) Presents At UBS Global Healthcare…(foreign_subject)
LHC Group (LHCG) Presents At UBS Global Healthcare Conference    (generic_ctx)
```

`flags = ["generic_name", "noisy", "thin_centric"]`

A model missing this is **not a model error** → report metrics
with and without flagged samples.

---

## What you can measure

| Dimension | Metrics | Ground truth |
|---|---|---|
| Verdict | detection · false alarms · lead days | label |
| Trajectory | risk-band agreement (strict / ±1) | derived day labels |
| Memory dedup | precision / recall | v0.2 clusters |
| Risk classification | per-type accuracy / recall | v0.4 silver labels |
| Data-quality slicing | with vs. without flagged | v0.3 flags |

**Reference baseline (test split):** 12/14 detected (12/12 excl. flagged) ·
mean lead **96 days** · 1/17 false alarms · dedup P=0.53/R=0.97 ·
lenient band agreement 0.95

---

## Known limitations

- Headlines only; no article bodies, no 8-K text
- event_date = 8-K filing date; media may be 1–3 days earlier
- negatives lack company names; "no bankruptcy **within** the window"
- `company.symbol` is inferred evidence, can be polluted (MF Global → IBKR)
- artificial ~1:1.2 class ratio
- name-matching noise annotated, not eliminated (6.0% rows, 5 samples)
- thin EDGAR coverage before 2004

---

## Summary

- A **streaming, memory-testing** credit-risk benchmark: 204 company windows, 17,277 headlines
- **Verified labels** (8-K bodies), **no lookahead**, real-world mess kept and annotated
- Four versions of **additive annotation**: dedup clusters → official split → noise audit → risk-type labels
- Quality problems are **found, measured and flagged** — not hidden

`data/credit_risk_dataset/` · README.md · DATASET_DESIGN.md
