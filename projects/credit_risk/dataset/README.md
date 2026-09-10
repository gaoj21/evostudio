> 当前发布：[2026-09-10-random-dev-test-v1](expansion/releases/2026-09-10-random-dev-test-v1/)。只分 dev（60 家、64 条轨迹）和 test（40 家、43 条轨迹），按 seed=42 随机划分公司并保留证据组隔离；各自输入在 observations.jsonl，结局在独立 outcomes.jsonl。Feed、Batch run 和 Evolve 均可选择版本和 dev/test；旧配置未指定版本时保留 contemporary。

> 当前 100 家公司版本见 [构建与数据说明](../docs/dataset/DATASET_CONSTRUCTION_AND_PROFILE.md)。构建脚本位于 `builders/`，下文保留历史版本说明。

# Credit Risk News Dataset

Evaluation dataset for agentic credit-risk analysis from news streams.
Each sample is one **company time-window**: ~180 days of news headlines
about a company, ending right before a known outcome (bankruptcy filing or
none). The task this dataset supports: given news arriving day by day,
maintain memory and produce a credit-risk verdict per company.

## Source & construction

| Piece | Source |
|---|---|
| Bankruptcy events (positives) | SEC EDGAR full-text search, 8-K filings matching `"Item 1.03"` (Bankruptcy or Receivership), first filing per CIK, 2004–2023 → `raw/bankruptcy_events.csv` (1,806 events) |
| Event verification | Each candidate event's 8-K **body** (parsed from the SGML full submission, first `<TYPE>8-K` document) is fetched from EDGAR and checked: `Item 1.03` must be an actual item heading AND bankruptcy keywords (chapter 7/11, voluntary petition, receivership, bankruptcy court) must appear. This kills FTS false positives like AT&T's credit-facility 8-K (its exhibit cites "Item 1.03" in default clauses). Subsidiary mentions are classified co-debtor (registrant + subsidiaries filed together — real) vs. review; the review bucket was manually audited (all real; the one subsidiary-only case, LendingTree, is overridden in `event_overrides.csv`) |
| News | FNSPID `Stock_news/All_external.csv` (Benzinga etc., 1999–2023). Title/URL/publisher/date only — the `Article` body column is mostly empty |
| Company ↔ news linking | Normalized company-name whole-token match against news title + URL (EDGAR does not retain tickers of delisted companies, so ticker mapping is not possible) |
| Match quality gate | A candidate is accepted only with evidence the match is real: **R1** one `Stock_symbol` covers ≥30% of its matched rows, **R2** the multi-token company key appears as a contiguous phrase in ≥50% of matched titles, or **R3** a distinctive single-token key (≥7 chars, not a common English word) appears in ≥60%. Rows are then cleaned (confirmed symbol OR phrase-in-title only). See `candidates_review.csv` |
| Negative samples | Alive `(symbol, year)` pairs with comparable news density, sampled 2:1 against positives, window ending Dec 20 of the sampled year; ETFs/funds blocklisted (their news is sector commentary, not company news) |

Build scripts (run in order):

```bash
.venv/bin/python projects/credit_risk/dataset/builders/build_01_events.py         # ~10 min, EDGAR FTS
.venv/bin/python projects/credit_risk/dataset/builders/build_02_match_news.py     # one pass over 5.7GB CSV
.venv/bin/python projects/credit_risk/dataset/builders/build_02b_verify_events.py # EDGAR 8-K body verification
.venv/bin/python projects/credit_risk/dataset/builders/build_03_samples.py        # writes v0.1/
.venv/bin/python projects/credit_risk/dataset/builders/build_04_v02.py            # v0.1 -> v0.2 (post-processing only)
.venv/bin/python projects/credit_risk/dataset/builders/build_05_v03.py            # v0.2 -> v0.3 (noise annotation only)
.venv/bin/python projects/credit_risk/dataset/builders/build_06_v04_risk_labels.py # v0.3 -> v0.4 (risk-type silver labels, needs DEEPSEEK_API_KEY)
```

## Schema (`v0.1/samples.jsonl`)

```json
{
  "sample_id": "pos_1318605_2010-06-01",
  "type": "positive | negative",
  "company": {"name": "...", "cik": "1318605", "symbol": "..."},
  "window": {"start": "2009-12-03", "end": "2010-05-31"},
  "label": {"event": "bankruptcy", "event_date": "2010-06-01"},
  "news": [
    {"news_id": "b3f1...", "date": "2010-03-14", "title": "...", "url": "...", "publisher": "..."}
  ]
}
```

- `window.end = event_date - 1` for positives: same-day news is excluded to
  prevent lookahead leakage (event-day coverage may report the filing).
  Verified: 0 leakage violations in v0.1 (re-checked on v0.2 output).
- `news_id` = first 16 hex chars of `sha1(date|title|url)`.
- v0.2 adds two fields: `dup_cluster_id` on each news row and `split` on
  each sample (see the v0.2 section below).
- Per-day `risk_level` labels are **derived**, not stored:

  | days_to_event | risk_level |
  |---|---|
  | ≤ 30 | critical |
  | 31–90 | high |
  | 91–180 | medium |
  | negative / no event | low |

  The same sample therefore supports both verdict-level evaluation
  (final risk call vs. outcome) and trajectory-level evaluation
  (risk escalation curve vs. days_to_event).

## v0.1 stats

See `v0.1/manifest.json`. ~92 positive company-windows (Lehman, WaMu, GM,
Tribune, Six Flags, Kodak, Borders, Sears, Hertz, PG&E, Chesapeake, Whiting,
…), ~110 negative windows, ~17k news rows.

## Known limitations

- **Headlines only**: FNSPID bodies are mostly empty, so samples carry
  headlines, not article text. 8-K filing content is out of scope for v0.1.
- **Duplicate rows from source**: FNSPID aggregates the same wire story from
  multiple publishers; ~12% exact (date, title) duplicates remain. Deliberately
  kept — near-duplicate detection is part of what the memory layer is
  evaluated on. **Annotated in v0.2** via `dup_cluster_id` (see below).
- **Negatives lack company identity**: chosen by ticker, so `company.name`
  is null for negatives; some tickers may have gone bankrupt *after* the
  sampled window (labels only assert "no bankruptcy within this window").
- **Bank-holding positives**: for a few regional banks (e.g. Colonial
  BancGroup, UCBH) the disclosed event is the FDIC receivership of the
  subsidiary bank(s); the holding company was wiped out but sometimes filed
  Chapter 11 days later — event_date can be off by days.
- **Class prior is artificial**: the ~1:1.2 positive:negative ratio does not
  reflect real-world bankruptcy frequency; it is chosen for evaluation
  balance. (Target was 1:2; some sampled negative windows fell below the
  30-news density bar.)
- EDGAR Item 1.03 coverage is thin before 2004 (FTS starts 2001, but
  8-K text availability is spotty until ~2004).

## v0.2 (`v0.2/`)

Built by `build_04_v02.py` from v0.1 (pure post-processing — same samples,
same windows, two additions):

**1. `dup_cluster_id` on every news row.** Near-duplicate clustering for
evaluating the dedup behavior of a memory layer. Per sample, union-find over
rows: merge on exact normalized-title equality (any dates — identical
recurring headlines like monthly `REG-...` notices count as duplicates), or
on token-set Jaccard ≥ 0.90 within ±3 days. Wire prefixes
(`UPDATE 2-`, `RPT-`, `WRAPUP 4-`, `CORRECTED-`, …) are stripped before
comparison. Cluster ids are sample-scoped (`c000`, `c001`, …) ordered by the
cluster's earliest date; singletons get their own id. Result: 17,277 rows →
14,584 clusters, 2,693 duplicate rows (15.6%); only 41 clusters contain
non-identical titles, and a manual sample of those checked out as true
duplicates (podcast title reorderings, source-suffix variants).

**2. Official train / dev / test split** — `split` field on each sample,
plus standalone `train.jsonl` / `dev.jsonl` / `test.jsonl`. Cut per class by
`window.end` at ~70/15/15 (per-class because bankruptcies cluster in crisis
years — one global date cut would leave dev with almost no positives). No
company (CIK or negative symbol) appears in two splits; this is asserted at
build time.

| split | samples | pos / neg | window.end range |
|---|---|---|---|
| train | 142 | 64 / 78 | 2007-10-16 .. 2018-12-20 |
| dev   | 31  | 14 / 17 | 2016-09-27 .. 2019-05-12 |
| test  | 31  | 14 / 17 | 2018-12-20 .. 2020-07-30 |

Note dev/test time ranges overlap with train's (and each other) by design of
the per-class cut — the guarantee is per-class chronological order plus
company disjointness, not disjoint global date ranges.

## v0.3 (`v0.3/`)

Built by `build_05_v03.py` from v0.2 (**annotation only** — samples, windows,
splits, row order and row counts are identical and asserted at build time,
so v0.2 evaluation baselines remain comparable). Motivation: pipeline
evaluation on v0.2 surfaced match-noise failure modes in a handful of news
streams (e.g. Bristow's stream is mostly gainers/losers roundups; Global
Healthcare REIT's stream matched "Global Healthcare **Conference**" headlines
about other companies).

**1. `noise_kind` on every news row** — `null` (company-centric) or one of:

| kind | meaning | example |
|---|---|---|
| `roundup` | daily gainers/losers/movers wire; company mentioned but no company-specific content | "American Midstream and Emerge Energy among Energy/Materials gainers; …" |
| `foreign_subject` | headline is about another company (foreign `(TICKER)` present, ours absent, our name not leading) | "Codexis (CDXS) Presents At UBS Global Healthcare Conference" |
| `person_mention` | company key token is a surname: officer-title pattern (`CEO Paul McDermott`) | "Washington REIT's (WRE) CEO Paul McDermott on Q2 Results" |
| `generic_ctx` | company key phrase is all-generic industry words (`generic_name` sample) and the row matched only via that phrase, no ticker evidence | "GW Pharmaceuticals to Present at the Goldman Sachs Global Healthcare Conference" |

Totals: 1,038 noise rows of 17,277 (6.0%) — 729 roundup, 259
foreign_subject, 50 generic_ctx, 0 person_mention after guards (the officer
heuristic's real hits, like the McDermott case, are already caught earlier by
`foreign_subject`). Heuristics, not ground truth: expect a few percent
error in both directions; spot-checked samples looked clean.

**2. `news_quality` on every sample** — `{"rows", "centric_rows",
"noise_share", "by_kind", "flags"}` with flags:

- `generic_name` — every key token of the company name is a generic
  industry/geography word; name matching is unreliable
- `noisy` — noise_share > 0.5
- `thin_centric` — < 15 company-centric rows in the window

5 samples are flagged (all positives): `pos_727346` Global Healthcare REIT
(99% noise, test), `pos_73887` Bristow (82%, dev), `pos_1555177` Emerge
Energy (72%, test), `pos_708819` McDermott (69%, test), `pos_931336` Dean
Foods (57%, test; the stream still holds 59 centric rows — flagged because
roundups dominate volume, not because it is unusable). Full list with
per-kind counts in `v0.3/manifest.json`.

**Usage**: for headline metrics, report both with and without flagged
samples (the v0.2 test-set baseline of the credit-risk pipeline, e.g., is
12/14 detections raw and 12/12 excluding the two flagged test samples known
at the time). For per-row filtering, drop `noise_kind != null` rows — after
filtering, every unflagged sample still has ≥ 24 centric rows.

## v0.4 (`v0.4/`)

Built by `build_06_v04_risk_labels.py` from v0.3 (annotation only — same
samples/rows/splits). Adds **`gold_risk_type`** on every news row: one of
the 8 event types of the pipeline's credit-risk taxonomy
(`examples/projects/credit_risk/skills/credit_risk_taxonomy`) when the headline
describes a credit-risk-relevant event about the sample's company, else
`null`. This gives ground truth for the pipeline's per-event risk
classification, which v0.3 could not score.

Method: deepseek-v4-flash labels (sample, date) batches with the taxonomy
in the prompt; `noise_kind` rows are auto-`null` without an LLM call; one
representative per `dup_cluster_id` is labeled and the label propagates to
cluster members (saves 15.6% of calls); per-sample checkpoints
(`v0.4/labels_ckpt/`) make the run resumable. Full run: ~1.9 h, $4.6.

Distribution over 17,277 rows (27.0% non-null):

| gold_risk_type | rows |
|---|---|
| positive_development | 1,334 |
| debt_default | 913 |
| liquidity_stress | 909 |
| lawsuit_regulatory | 635 |
| earnings_warning | 394 |
| operational_shock | 206 |
| rating_downgrade | 150 |
| management_turmoil | 117 |

These are **silver labels** (LLM pre-labeled, spot-checked per type, not
human gold): expect single-digit % noise — e.g. analyst downgrades
occasionally land in `rating_downgrade` though the taxonomy means rating
agencies. Use for per-type recall / accuracy metrics, not for auditing
individual rows.

## v0.5 TODO

- Scale up positives: loosen `MIN_NEWS` to 20 or extend the window to 365d.
- Optional: SEC 8-K filing text as an additional signal source.
- Give negatives real company identity (resolve symbol → company name) so
  `company.name` is populated for all samples.
