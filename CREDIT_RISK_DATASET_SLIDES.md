# Credit Risk Monitoring Dataset — Slide Content

Final release: `2026-09-14-eligible-trajectories-v1`

Updated: September 14, 2026

## Slide 1 — Dataset Objective

### Credit Risk Monitoring Through Company Trajectories

The dataset evaluates whether an agent can identify emerging credit risk before a verified event by tracking a company's public information over time.

- **Evaluation unit:** One company observation window, or trajectory.
- **Inputs:** News and public filings ordered by publication date.
- **Agent task:** Accumulate evidence, update its assessment, and issue an early warning.
- **Ground truth:** Independently stored event type, date, and entity scope; no daily risk-level labels.

**Public information → Sequential observations → Memory updates → Risk warning → Event-based evaluation**

## Slide 2 — Dataset Construction

1. **Candidate identification:** Compile company identities, CIK identifiers, candidate events, and observation windows.
2. **Evidence collection:** Combine GDELT news discovery, cached news articles, and SEC filings, including Forms 8-K and 8-K/A. Use targeted web searches to supplement event verification.
3. **Entity and temporal alignment:** Match evidence to the company, restrict inputs by publication date, remove duplicates, and clean noisy text.
4. **Trajectory assembly:** Group materials published on the same day into an observation. Days without new evidence do not create empty observations.
5. **Eligibility filtering:** Retain **53 of 107 candidate trajectories**, excluding 54 that do not meet the current evaluation criteria.

Retained trajectories require a verified target bankruptcy event involving the registrant, with the event date strictly after the observation window ends.

Excluded trajectories include insufficiently verified cases and windows designed for other monitoring objectives; exclusion does not necessarily mean their underlying evidence is incorrect.

## Slide 3 — Final Dataset Profile

| Metric | Total | Dev | Test |
|---|---:|---:|---:|
| Companies | **53** | 30 | 23 |
| Trajectories | **53** | 30 | 23 |
| Observations | **648** | 397 | 251 |

**1,006 unique evidence documents:**

| Evidence representation | Count |
|---|---:|
| Cached news article bodies | 376 |
| Headline-only records | 305 |
| Cached filing bodies | 324 |
| Assistant-written paraphrase of a primary source | 1 |

The dataset covers **6 broad industry groups and 36 SIC codes**, with manufacturing accounting for 26 of the 53 companies.

**The 53 trajectories are evaluation cases; the 648 observations are time steps within those cases.**

## Slide 4 — Splits and Event Labels

The parent dataset used a company-level random Dev/Test split with **seed 42**, subject to shared-evidence groups remaining within the same split.

The final release preserves the original assignments after eligibility filtering:

- **Dev:** 30 trajectories for development and prompt evolution.
- **Test:** 23 trajectories for evaluation.
- The resulting split is approximately **57:43**, rather than exactly 60:40.
- Outcome labels are stored separately and excluded from model inputs.

| Verified target event | Trajectories |
|---|---:|
| Chapter 11 bankruptcy | 44 |
| Chapter 7 bankruptcy | 8 |
| Canadian bankruptcy assignment | 1 |

If Test data have informed prompt changes, that use should be disclosed rather than describing the split as untouched.

## Slide 5 — Evaluation Scope and Limitations

The dataset supports evaluation of:

- **Event detection rate:** The proportion of eligible trajectories receiving a valid warning, with missed events retained in the denominator.
- **Warning lead time:** The interval between the first valid warning and the verified event.
- **Evidence quality:** Whether warnings rely on relevant information available at the time.
- **Memory contribution:** Differences in detection and lead time under matched runs with and without memory.

Limitations:

- All 53 trajectories are positive cases; there are no verified negative cases.
- Overall binary classification accuracy and false-positive rate cannot be assessed.
- Evidence density varies, some records contain only headlines, and industries are unevenly represented.
- Eligibility filtering does not constitute an exhaustive evidence-quality audit.
- The current Evolve implementation uses end-of-window snapshots; its scores do not directly measure sequential memory or early-warning performance.

## Short Presentation Script

> We constructed a company-level credit risk trajectory dataset from public news and SEC filings. Following entity matching, temporal alignment, deduplication, and event verification, the final release contains 53 company trajectories, 648 observations, and 1,006 unique evidence documents. We retain 30 trajectories for development and 23 for testing. The dataset is designed to evaluate whether agents can accumulate evidence and identify risk before a verified event, and whether memory improves detection and warning lead time. It is a positive-only dataset and does not support evaluation of overall binary classification accuracy.

## Repository References

- [Final dataset directory](projects/credit_risk/dataset/expansion/releases/2026-09-14-eligible-trajectories-v1/)
- [Release manifest: statistics, eligibility rules, and checksums](projects/credit_risk/dataset/expansion/releases/2026-09-14-eligible-trajectories-v1/manifest.json)
- [Detailed current dataset profile (Chinese)](projects/credit_risk/docs/dataset/DATASET_CURRENT_PROFILE.md)
- [Historical construction record (Chinese)](projects/credit_risk/docs/dataset/DATASET_CONSTRUCTION_AND_PROFILE.md)
