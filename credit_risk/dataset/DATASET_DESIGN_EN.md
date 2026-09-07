# Credit Risk News Dataset · Design Document

A dataset for evaluating **agentic credit-risk analysis**: each sample is one
company's **day-by-day stream of news headlines over a 180-day window**,
ending right before a known outcome (bankruptcy / none). The model must
ingest news daily, maintain memory, and issue a risk verdict per day —
exactly like a real deployment.

- Location: `data/credit_risk_dataset/` (current: `v0.4/`)
- Size: 204 samples (92 positive / 112 negative), 17,277 news rows,
  events spanning 2007–2020
- Authoritative field reference: `README.md`; work chronicle:
  `DATASET_WORKLOG.md`; 中文设计文档: `DATASET_DESIGN.md`

---

## 1. Task definition

```
Input:  a company + a date-ordered stream of news headlines (0..N per day)
Output: one risk verdict per day {risk_level, score, trend, key_evidence}
Eval:   in-window verdicts vs. the known outcome right after the window
```

Why it is designed this way:

- **Streaming + memory is the capability under test.** Real credit
  monitoring is not one-shot classification; state evolves day by day as
  news arrives. Samples keep every intermediate day, not just a final
  window-end judgment.
- **Headlines only.** The source (FNSPID) has mostly empty article bodies.
  This constraint is also realistic: headline-level signal is weak enough to
  actually exercise memory aggregation and denoising.
- **No lookahead.** Positive windows are `[event_date - 180d,
  event_date - 1d]` — event-day news is excluded entirely (same-day coverage
  would report the filing itself). 0 leakage violations, re-checked on every
  rebuild.

## 2. Sources & construction pipeline

| Stage | Source / method |
|---|---|
| Bankruptcy events (positive labels) | SEC EDGAR full-text search: 8-K filings matching `Item 1.03` (Bankruptcy or Receivership), first filing per CIK, 2004–2023 → 1,806 candidates |
| Event verification | Fetch each candidate's 8-K **body**: `Item 1.03` must be an actual item heading AND bankruptcy keywords must appear (chapter 7/11, petition, receivership). This kills FTS false positives (e.g. an AT&T credit-facility 8-K whose exhibit cites "Item 1.03" in default clauses). Subsidiary co-filings manually audited; the single subsidiary-only case (LendingTree) is overridden in `event_overrides.csv` |
| News | FNSPID `All_external.csv` (Benzinga et al., 1999–2023, 5.7GB): title/URL/publisher/date only |
| Company ↔ news linking | Normalized company-name whole-token match over title + URL. **Tickers cannot be used**: EDGAR does not retain tickers of delisted companies |
| Match quality gate | A candidate needs at least one piece of evidence: **R1** one stock symbol covers ≥30% of matched rows; **R2** the multi-token company key appears as a contiguous phrase in ≥50% of matched titles; **R3** a distinctive single-token key (≥7 chars, non-common word) appears in ≥60%. Row-level cleanup on top. Manual review logged in `candidates_review.csv` |
| Negatives | Alive (symbol, year) pairs with comparable news density (≥30 rows/window), sampled 2:1 against positives, window ending Dec 20 of the sampled year; ETFs/funds blocklisted (their news is sector commentary, not company news) |

Build scripts (in order): `build_01_events.py` (EDGAR FTS) →
`build_02_match_news.py` (one pass over 5.7GB CSV) →
`build_02b_verify_events.py` (8-K body verification) →
`build_03_samples.py` (writes v0.1) → `build_04_v02.py` →
`build_05_v03.py` → `build_06_v04_risk_labels.py`.

## 3. Schema

```json
{
  "sample_id": "pos_1401106_2011-11-03",
  "type": "positive",
  "company": {"name": "MF Global Holdings Ltd.", "cik": "1401106", "symbol": "..."},
  "window": {"start": "2011-05-07", "end": "2011-11-02"},
  "label": {"event": "bankruptcy", "event_date": "2011-11-03"},
  "split": "train",
  "news_quality": {                    // added in v0.3
    "rows": 290, "centric_rows": 286, "noise_share": 0.014,
    "by_kind": {"roundup": 4}, "flags": []
  },
  "news": [
    {
      "news_id": "b3f1...",            // first 16 hex of sha1(date|title|url)
      "date": "2011-10-27",
      "title": "Fitch downgrades MF Global; cites risk-taking",
      "url": "...", "publisher": "...",
      "dup_cluster_id": "c187",        // added in v0.2: near-duplicate cluster
      "noise_kind": null,              // added in v0.3: noise category
      "gold_risk_type": "rating_downgrade"   // added in v0.4: risk-type silver label
    }
  ]
}
```

Negatives: `company.name/cik` are null (selected by symbol);
`label.event` is null, asserting only "no bankruptcy within the window"
(not after it).

**Derived day-level labels** (computed, not stored): ≤30 days to event →
critical / 31–90 → high / 91–180 → medium / negative → low. Each sample
therefore supports both verdict-level evaluation (final risk call vs.
outcome) and trajectory-level evaluation (escalation curve vs. countdown).

## 4. Version evolution

| Version | Content | Motivation |
|---|---|---|
| v0.1 | 204 samples built (pipeline above) | — |
| v0.2 | + per-row `dup_cluster_id` (near-duplicate annotation); + official train/dev/test split | Dedup is a tested capability of the memory layer and needs ground truth; optimization experiments need a fixed split |
| v0.3 | + per-row `noise_kind`; + per-sample `news_quality` with flags | Pipeline evaluation showed 2 missed detections were **data noise**, not model errors → audit the whole dataset |
| v0.4 | + per-row `gold_risk_type` (8-class risk taxonomy, LLM silver labels) | The pipeline tags extracted events with event_type, but without ground truth that classification could not be scored |

Every version is **pure additive annotation**: identical samples, windows,
row order and row counts, asserted at build time — old baselines stay
comparable forever.

### v0.2 dedup annotation

Per-sample union-find: merge on exact normalized-title equality (any dates —
recurring identical headlines like monthly notices count as duplicates), or
token-set Jaccard ≥ 0.90 within ±3 days; wire prefixes (`UPDATE 2-`, `RPT-`,
`CORRECTED-`, …) stripped first. Result: 17,277 rows → 14,584 clusters,
2,693 duplicate rows (15.6%). **Duplicates are deliberately kept** — that is
what the source looks like, and near-duplicate detection is part of what the
memory layer is evaluated on.

### v0.2 official split

Per-class chronological cut by `window.end`, 70/15/15 (per-class because
bankruptcies cluster in crisis years — one global date cut would leave dev
with almost no positives). No company appears in two splits; asserted at
build time.

| split | samples | pos/neg | news rows |
|---|---|---|---|
| train | 142 | 64/78 | 12,253 |
| dev | 31 | 14/17 | 2,696 |
| test | 31 | 14/17 | 2,328 |

### v0.3 noise audit (4 `noise_kind` categories)

| kind | meaning | rows |
|---|---|---|
| `roundup` | daily gainers/losers wire; company mentioned but no company-specific content | 729 |
| `foreign_subject` | headline is about another company (their ticker present, ours absent) | 259 |
| `generic_ctx` | row of a `generic_name` sample matched only via a generic industry phrase | 50 |
| `person_mention` | company key is a surname; officer-title pattern ("CEO Paul McDermott") | ~0 (real hits are already caught by `foreign_subject`) |

Total 1,038 noise rows (6.0%); the dataset-wide median noise share is only
2.8% — noise is highly concentrated. Sample-level flags: `generic_name` /
`noisy` (noise share > 50%) / `thin_centric` (< 15 company-centric rows).
**5 samples are flagged** (all positives): Global Healthcare REIT (99%),
Bristow (82%), Emerge Energy (72%), McDermott (69%), Dean Foods (57%).

### v0.4 risk-type labels

Closed taxonomy (8 classes, shared verbatim with the pipeline's extraction
node): `debt_default` / `rating_downgrade` / `liquidity_stress` /
`lawsuit_regulatory` / `earnings_warning` / `management_turmoil` /
`operational_shock` / `positive_development`.

Method: deepseek-v4-flash labels (sample, date) batches; `noise_kind` rows
are auto-null without an LLM call; one representative per `dup_cluster_id`
is labeled and the label propagates (saves 15.6% of calls); per-sample
checkpoints make the run resumable. Full run: ~1.9 h, $4.6.
Result: 4,658 of 17,277 rows (27.0%) carry a non-null label —
positive_development 1,334 / debt_default 913 / liquidity_stress 909 /
lawsuit_regulatory 635 / earnings_warning 394 / operational_shock 206 /
rating_downgrade 150 / management_turmoil 117.
These are **silver labels** (LLM pre-labeled, spot-checked per type, not
human gold), intended for scoring the pipeline's risk-classification
accuracy.

## 5. Examples

### Example 1: positive — MF Global (pos_1401106, train)

Label: bankruptcy, 8-K Item 1.03 filed 2011-11-03; window 2011-05-07 ~
11-02; 290 rows; noise share 1.4% (clean sample). gold_risk_type
distribution: liquidity_stress 41 / debt_default 36 / lawsuit_regulatory 32
/ positive_development 20 / rating_downgrade 8 / a few others.

A textbook escalation arc (gold_risk_type in parentheses):

```
2011-05-19  MF Global Posts Loss as Legal, Operational Costs Hurt Profit   (earnings_warning)
2011-09-26  MF Global Fined for Market Manipulation Over Fortis in 2008    (lawsuit_regulatory)
2011-10-16  Regulator directed MF Global to boost its capital: WSJ         (liquidity_stress)
2011-10-25  UPDATE 3-MF Global posts Q2 loss on market volatility          (earnings_warning)
2011-10-26  UPDATE 1-S&P may cut MF Global rating to junk                  (rating_downgrade)
2011-10-27  Some MF Global clients move money away as troubles grow        (liquidity_stress)
2011-10-27  Fitch downgrades MF Global; cites risk-taking                  (rating_downgrade)
2011-10-28  MF Global aims for sale by Monday: source                      (liquidity_stress)
2011-10-31  MF Global Files for Chapter 11                                 (debt_default)
2011-10-31  London Metal Exchange suspends MF Global from trading          (operational_shock)
2011-11-01  CME: MF Global Not In Compliance With Customer Fund Rules      (lawsuit_regulatory)
2011-11-02  CME says MF Global customer shortfall of $633 mln              (liquidity_stress)
```

Liquidity signals appear ~90 days out, rating downgrades ~31 days out, and
coverage explodes in the final 3 days — matching the medium→high→critical
trajectory bands. (Note: `event_date` is the 8-K filing date, 11-03; media
reported the filing on 10-31. A few days of slack is a known property — see
Limitations.)

### Example 2: negative — Micron (neg_MU_2018, train)

Label: no event; window 2018-06-23 ~ 12-20; 201 rows; noise share 7%.
`company.name` is null (negatives are symbol-selected). A normal company
news stream:

```
2018-06-26  UBS Upgrades Micron Technology to Neutral
2018-06-26  UBS Upgrades Micron to Neutral from Sell; Raises PT to $60   ← near-dup of the line above (same dup_cluster_id)
2018-06-26  DRAMeXchange Says Server DRAM Supply to Improve in Q3
```

Plus distractors: "Bulls & Bears Of The Week", "26 Stocks Moving In
Tuesday's Pre-Market Session" (the latter is a recurring identical title —
a dup cluster spanning different dates). Negatives exercise dedup and noise
resistance too.

### Example 3: flagged noisy sample — Global Healthcare REIT (pos_727346, test)

Only 1 of 72 rows is truly company-relevant; `news_quality.flags =
["generic_name", "noisy", "thin_centric"]`. The company name contains the
generic industry phrase "Global Healthcare", so matching pulled in **other
companies'** conference news:

```
2019-05-22  Codexis (CDXS) Presents At UBS Global Healthcare Conference        (noise: generic_ctx)
2019-05-23  Community Health Systems (CYH) Presents At UBS Global Healthcare…  (noise: foreign_subject)
2019-05-23  LHC Group (LHCG) Presents At UBS Global Healthcare Conference      (noise: generic_ctx)
```

Such samples are fundamental matching failures — a model missing them is
not a model error. Evaluations should report metrics both with and without
flagged samples.

## 6. Evaluation usage

| Dimension | Metrics | Depends on |
|---|---|---|
| Verdict | positive detection rate, false-alarm rate, early-warning lead days | label |
| Trajectory | risk-band agreement (strict / lenient ±1 band) | derived day labels |
| Memory dedup | dedup precision / recall | v0.2 `dup_cluster_id` |
| Risk classification | event_type accuracy / per-class recall | v0.4 `gold_risk_type` |
| Data-quality slicing | report with vs. without flagged samples | v0.3 `news_quality` |

Reference baseline (this repo's pipeline, test split): 12/14 positives
detected (12/12 excluding flagged), mean lead time 96 days, 1/17 false
alarms, dedup P=0.53 / R=0.97, lenient risk-band agreement 0.95.

## 7. Known limitations

- Headlines only, no article bodies; 8-K full text is out of scope
  (v0.4+ candidate)
- 15.6% duplicate rows from the source, kept deliberately (annotated)
- Negatives lack company identity; labels assert only "no bankruptcy within
  the window"
- For a few bank-holding/financial companies (Colonial, UCBH, MF Global)
  the event_date is the 8-K filing date; media may report the filing 1–3
  days earlier
- `company.symbol` is inferred from match evidence and can be polluted
  (e.g. MF Global shows IBKR — its would-be acquirer dominates the matched
  rows); treat it as a hint, not truth
- The ~1:1.2 positive:negative ratio is artificial, not real-world
  bankruptcy frequency
- Name-matching noise: 6.0% of rows and 5 samples annotated in v0.3;
  heuristic annotations and LLM silver labels both carry a few % error in
  either direction
- EDGAR Item 1.03 coverage is thin before 2004

## 8. Reproduce

```bash
.venv/bin/python data/credit_risk_dataset/build_01_events.py          # ~10 min, EDGAR FTS
.venv/bin/python data/credit_risk_dataset/build_02_match_news.py      # one pass over 5.7GB CSV
.venv/bin/python data/credit_risk_dataset/build_02b_verify_events.py  # 8-K body verification
.venv/bin/python data/credit_risk_dataset/build_03_samples.py         # -> v0.1/
.venv/bin/python data/credit_risk_dataset/build_04_v02.py             # -> v0.2/ (dedup + split)
.venv/bin/python data/credit_risk_dataset/build_05_v03.py             # -> v0.3/ (noise audit)
.venv/bin/python data/credit_risk_dataset/build_06_v04_risk_labels.py # -> v0.4/ (risk-type silver labels; needs DEEPSEEK_API_KEY)
```
